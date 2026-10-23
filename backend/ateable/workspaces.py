"""Workspace membership, participant APIs and durable metadata collaboration.

Participants can request remote downloads and exchange plain messages. They
cannot manage workers, controller settings, credentials, or filesystem contents.
Queue requests, placement and scope checks share one SQLite transaction.
"""

import hashlib
import hmac
import json
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as ModelError

from .database import ConflictError, Database, NotFoundError, utc_now
from .schemas import MediaFormat
from .validation import (
    ValidationError,
    is_s3_listing,
    normalize_download_url,
    parse_s3_url,
    validate_output_subdir,
)


class AccessDenied(PermissionError):
    pass


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkspaceCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)


class WorkspacePatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)


class MemberCreate(StrictModel):
    worker_id: str = Field(min_length=1, max_length=200)


class AgentCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)


class DownloadRequest(StrictModel):
    urls: list[Annotated[str, Field(min_length=1, max_length=8192)]] = Field(
        min_length=1, max_length=5000
    )
    format: MediaFormat = "original"
    target_worker_id: str | None = Field(default="auto", max_length=200)
    output_subdir: str | None = Field(default=None, max_length=240)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)


class MessageCreate(StrictModel):
    text: str = Field(min_length=1, max_length=10000)
    recipient_id: str | None = Field(default=None, min_length=1, max_length=200)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)


@dataclass(frozen=True)
class Participant:
    id: str
    type: str
    name: str
    token_digest: str

    def public(self) -> dict[str, str]:
        return {"id": self.id, "type": self.type, "name": self.name}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _model(model: type[BaseModel], payload: Any) -> Any:
    try:
        return model.model_validate(payload)
    except ModelError as exc:
        errors = [
            {"loc": error["loc"], "message": error["msg"]}
            for error in exc.errors(include_input=False, include_context=False)
        ]
        raise ValidationError(_json(errors)) from exc


def _snapshot_sql(prefix: str) -> str:
    fields = (
        "id",
        "batch_id",
        "source_url",
        "canonical_url",
        "kind",
        "source_job_id",
        "root_job_id",
        "discovery_count",
        "format",
        "status",
        "progress",
        "stage",
        "title",
        "target_worker_id",
        "claimed_by",
        "attempts",
        "max_attempts",
        "output_path",
        "error",
        "created_at",
        "updated_at",
    )
    return (
        "json_object("
        + ",".join(f"'{field}',{prefix}.{field}" for field in fields)
        + f",'task_options',json({prefix}.task_options))"
    )


class WorkspaceStore:
    def __init__(self, database: Database):
        self.database = database
        self.initialize()

    def initialize(self) -> None:
        with self.database.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY,name TEXT NOT NULL,name_key TEXT UNIQUE NOT NULL,
                    description TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workspace_members (
                    workspace_id TEXT NOT NULL,worker_id TEXT NOT NULL,created_at TEXT NOT NULL,
                    PRIMARY KEY(workspace_id,worker_id),
                    FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
                    FOREIGN KEY(worker_id) REFERENCES workers(id)
                );
                CREATE TABLE IF NOT EXISTS workspace_agents (
                    id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,name TEXT NOT NULL,
                    token_hash TEXT UNIQUE NOT NULL,created_at TEXT NOT NULL,revoked_at TEXT,
                    FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
                );
                CREATE TABLE IF NOT EXISTS workspace_download_requests (
                    id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,actor_id TEXT NOT NULL,
                    actor_type TEXT NOT NULL,idempotency_key TEXT,request_hash TEXT NOT NULL,
                    batch_id TEXT NOT NULL,options TEXT NOT NULL,duplicates TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(workspace_id,actor_type,actor_id,idempotency_key),
                    FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
                );
                CREATE TABLE IF NOT EXISTS workspace_jobs (
                    job_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,request_id TEXT NOT NULL,
                    root_job_id TEXT NOT NULL,status TEXT NOT NULL,kind TEXT NOT NULL,stage TEXT,
                    title TEXT,source_url TEXT NOT NULL,canonical_url TEXT NOT NULL,
                    worker_id TEXT,job_json TEXT NOT NULL,queue_removed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                    FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
                    FOREIGN KEY(request_id) REFERENCES workspace_download_requests(id)
                );
                CREATE INDEX IF NOT EXISTS idx_workspace_jobs ON workspace_jobs(workspace_id,created_at,job_id);
                CREATE INDEX IF NOT EXISTS idx_workspace_roots ON workspace_jobs(root_job_id);
                CREATE INDEX IF NOT EXISTS idx_workspace_request_roots ON workspace_jobs(request_id)
                    WHERE root_job_id=job_id;
                CREATE TABLE IF NOT EXISTS workspace_messages (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,workspace_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,sender_type TEXT NOT NULL,sender_name TEXT NOT NULL,
                    recipient_id TEXT,recipient_type TEXT,text TEXT NOT NULL,idempotency_key TEXT,
                    request_hash TEXT NOT NULL,created_at TEXT NOT NULL,
                    UNIQUE(workspace_id,sender_type,sender_id,idempotency_key),
                    FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
                );
                CREATE INDEX IF NOT EXISTS idx_workspace_messages ON workspace_messages(workspace_id,seq);
            """)
            # The insert trigger links roots and discovery descendants inside
            # their queue transaction; snapshots retain history after cleanup.
            connection.executescript(f"""
                CREATE TRIGGER IF NOT EXISTS workspace_job_insert AFTER INSERT ON jobs
                BEGIN
                    INSERT OR IGNORE INTO workspace_jobs(
                        job_id,workspace_id,request_id,root_job_id,status,kind,stage,title,source_url,
                        canonical_url,worker_id,job_json,created_at,updated_at)
                    SELECT NEW.id,r.workspace_id,r.id,COALESCE(NEW.root_job_id,NEW.id),NEW.status,
                        NEW.kind,NEW.stage,NEW.title,NEW.source_url,NEW.canonical_url,
                        NEW.target_worker_id,{_snapshot_sql("NEW")},NEW.created_at,NEW.updated_at
                    FROM workspace_download_requests r WHERE r.batch_id=NEW.batch_id OR EXISTS(
                        SELECT 1 FROM workspace_jobs p WHERE p.request_id=r.id AND p.job_id=NEW.root_job_id);
                END;
                CREATE TRIGGER IF NOT EXISTS workspace_job_update AFTER UPDATE ON jobs
                BEGIN
                    UPDATE workspace_jobs SET status=NEW.status,kind=NEW.kind,stage=NEW.stage,title=NEW.title,
                        worker_id=NEW.target_worker_id,job_json={_snapshot_sql("NEW")},updated_at=NEW.updated_at
                    WHERE job_id=NEW.id;
                END;
                CREATE TRIGGER IF NOT EXISTS workspace_job_delete BEFORE DELETE ON jobs
                BEGIN
                    UPDATE workspace_jobs SET status=OLD.status,kind=OLD.kind,stage=OLD.stage,title=OLD.title,
                        worker_id=OLD.target_worker_id,job_json={_snapshot_sql("OLD")},
                        updated_at=OLD.updated_at,queue_removed=1 WHERE job_id=OLD.id;
                END;
            """)

    @staticmethod
    def _workspace(connection: sqlite3.Connection, workspace_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM workspaces WHERE id=?", (workspace_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("Workspace was not found")
        return row

    @staticmethod
    def _public_workspace(
        connection: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        return {
            key: row[key]
            for key in ("id", "name", "description", "created_at", "updated_at")
        } | {
            "member_count": connection.execute(
                "SELECT COUNT(*) FROM workspace_members WHERE workspace_id=?",
                (row["id"],),
            ).fetchone()[0],
            "agent_count": connection.execute(
                "SELECT COUNT(*) FROM workspace_agents WHERE workspace_id=? AND revoked_at IS NULL",
                (row["id"],),
            ).fetchone()[0],
        }

    def authenticate(
        self,
        token: str | None,
        *,
        worker_id: str | None = None,
        agent_id: str | None = None,
    ) -> Participant:
        if not token or len(token) > 256 or bool(worker_id) == bool(agent_id):
            raise PermissionError(
                "Participant bearer and exactly one identity header are required"
            )
        identity = worker_id or agent_id
        if len(identity) > 200 or identity == "local":
            raise PermissionError("Invalid participant credentials")
        with self.database.connect() as connection:
            if worker_id:
                row = connection.execute(
                    "SELECT name,token_hash FROM workers WHERE id=? AND id!='local'",
                    (worker_id,),
                ).fetchone()
                actor_type = "worker"
            else:
                row = connection.execute(
                    "SELECT name,token_hash FROM workspace_agents WHERE id=? AND revoked_at IS NULL",
                    (agent_id,),
                ).fetchone()
                actor_type = "agent"
        digest = _hash(token)
        if not row or not hmac.compare_digest(row["token_hash"], digest):
            raise PermissionError("Invalid participant credentials")
        return Participant(identity, actor_type, row["name"], digest)

    def _scope(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        actor: Participant | None,
    ) -> None:
        self._workspace(connection, workspace_id)
        if actor is None:
            return
        if actor.type == "worker":
            row = connection.execute(
                "SELECT w.token_hash FROM workers w JOIN workspace_members m "
                "ON m.worker_id=w.id WHERE m.workspace_id=? AND w.id=? AND w.id!='local'",
                (workspace_id, actor.id),
            ).fetchone()
        elif actor.type == "agent":
            row = connection.execute(
                "SELECT token_hash FROM workspace_agents WHERE workspace_id=? AND id=? AND revoked_at IS NULL",
                (workspace_id, actor.id),
            ).fetchone()
        else:
            row = None
        if not row or not hmac.compare_digest(row["token_hash"], actor.token_digest):
            raise AccessDenied("Participant is not a current member of this workspace")

    def list_workspaces(self, actor: Participant | None = None) -> dict[str, Any]:
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            if actor is None:
                rows = connection.execute(
                    "SELECT * FROM workspaces ORDER BY created_at,id"
                ).fetchall()
            elif actor.type == "worker":
                rows = connection.execute(
                    "SELECT s.* FROM workspaces s JOIN workspace_members m ON m.workspace_id=s.id "
                    "WHERE m.worker_id=? ORDER BY s.created_at,s.id",
                    (actor.id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT s.* FROM workspaces s JOIN workspace_agents a ON a.workspace_id=s.id "
                    "WHERE a.id=? AND a.revoked_at IS NULL ORDER BY s.created_at,s.id",
                    (actor.id,),
                ).fetchall()
            for row in rows:
                self._scope(connection, row["id"], actor)
            items = [self._public_workspace(connection, row) for row in rows]
        result = {"items": items, "total": len(items)}
        if actor:
            result["actor"] = actor.public()
        return result

    def create_workspace(self, name: str, description: str = "") -> dict[str, Any]:
        payload = _model(WorkspaceCreate, {"name": name, "description": description})
        name = payload.name.strip()
        if not name:
            raise ValidationError("Workspace name is empty")
        workspace_id, now = str(uuid.uuid4()), utc_now()
        try:
            with self.database.connect() as connection:
                connection.execute(
                    "INSERT INTO workspaces(id,name,name_key,description,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (
                        workspace_id,
                        name,
                        name.casefold(),
                        payload.description,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("A workspace with this name already exists") from exc
        return self.get_workspace(workspace_id)

    def get_workspace(
        self, workspace_id: str, actor: Participant | None = None
    ) -> dict[str, Any]:
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            self._scope(connection, workspace_id, actor)
            return self._public_workspace(
                connection, self._workspace(connection, workspace_id)
            )

    def update_workspace(
        self,
        workspace_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        _model(WorkspacePatch, {"name": name, "description": description})
        if name is not None:
            name = name.strip()
            if not name:
                raise ValidationError("Workspace name is empty")
        try:
            with self.database.connect() as connection:
                self._workspace(connection, workspace_id)
                connection.execute(
                    "UPDATE workspaces SET name=COALESCE(?,name),name_key=COALESCE(?,name_key),"
                    "description=COALESCE(?,description),updated_at=? WHERE id=?",
                    (
                        name,
                        name.casefold() if name else None,
                        description,
                        utc_now(),
                        workspace_id,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("A workspace with this name already exists") from exc
        return self.get_workspace(workspace_id)

    def add_member(self, workspace_id: str, worker_id: str) -> dict[str, Any]:
        _model(MemberCreate, {"worker_id": worker_id})
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._workspace(connection, workspace_id)
            worker = connection.execute(
                "SELECT id FROM workers WHERE id=? AND id!='local'", (worker_id,)
            ).fetchone()
            if not worker:
                raise NotFoundError("Enroll a paired remote worker")
            connection.execute(
                "INSERT OR IGNORE INTO workspace_members(workspace_id,worker_id,created_at) VALUES(?,?,?)",
                (workspace_id, worker_id, utc_now()),
            )
            connection.commit()
        return {"workspace_id": workspace_id, "worker_id": worker_id, "enrolled": True}

    def remove_member(self, workspace_id: str, worker_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._workspace(connection, workspace_id)
            connection.execute(
                "DELETE FROM workspace_members WHERE workspace_id=? AND worker_id=?",
                (workspace_id, worker_id),
            )
            connection.commit()
        return {"workspace_id": workspace_id, "worker_id": worker_id, "removed": True}

    def _worker_threshold(self, connection: sqlite3.Connection) -> str:
        setting = connection.execute(
            "SELECT value FROM settings WHERE key='worker_timeout_seconds'"
        ).fetchone()
        timeout = (
            int(setting[0]) if setting else self.database.config.worker_timeout_seconds
        )
        return (datetime.now(UTC) - timedelta(seconds=timeout)).isoformat(
            timespec="milliseconds"
        )

    def list_members(
        self, workspace_id: str, actor: Participant | None = None
    ) -> dict[str, Any]:
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            self._scope(connection, workspace_id, actor)
            threshold = self._worker_threshold(connection)
            rows = connection.execute(
                "SELECT w.id,w.name,w.last_seen_at FROM workers w JOIN workspace_members m "
                "ON m.worker_id=w.id WHERE m.workspace_id=? AND w.id!='local' ORDER BY w.name,w.id",
                (workspace_id,),
            ).fetchall()
            items = [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "status": "online"
                    if row["last_seen_at"] >= threshold
                    else "offline",
                }
                for row in rows
            ]
            agents = [
                {"id": row["id"], "name": row["name"], "status": "active"}
                for row in connection.execute(
                    "SELECT id,name FROM workspace_agents WHERE workspace_id=? AND revoked_at IS NULL ORDER BY name,id",
                    (workspace_id,),
                ).fetchall()
            ]
        return {"items": items, "agents": agents, "total": len(items)}

    def create_agent(self, workspace_id: str, name: str) -> dict[str, Any]:
        name = _model(AgentCreate, {"name": name}).name.strip()
        if not name:
            raise ValidationError("Agent name is empty")
        agent_id, token, now = (
            str(uuid.uuid4()),
            "awsp_" + secrets.token_urlsafe(36),
            utc_now(),
        )
        with self.database.connect() as connection:
            self._workspace(connection, workspace_id)
            connection.execute(
                "INSERT INTO workspace_agents(id,workspace_id,name,token_hash,created_at) VALUES(?,?,?,?,?)",
                (agent_id, workspace_id, name, _hash(token), now),
            )
        return {
            "id": agent_id,
            "agent_id": agent_id,
            "workspace_id": workspace_id,
            "name": name,
            "token": token,
            "created_at": now,
            "revoked_at": None,
        }

    def list_agents(self, workspace_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            self._workspace(connection, workspace_id)
            rows = connection.execute(
                "SELECT id,workspace_id,name,created_at,revoked_at FROM workspace_agents WHERE workspace_id=? ORDER BY created_at,id",
                (workspace_id,),
            ).fetchall()
        return {
            "items": [dict(row) | {"agent_id": row["id"]} for row in rows],
            "total": len(rows),
        }

    def revoke_agent(self, workspace_id: str, agent_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            self._workspace(connection, workspace_id)
            changed = connection.execute(
                "UPDATE workspace_agents SET revoked_at=COALESCE(revoked_at,?) WHERE workspace_id=? AND id=?",
                (utc_now(), workspace_id, agent_id),
            ).rowcount
            if not changed:
                raise NotFoundError("Agent was not found in this workspace")
        return {"workspace_id": workspace_id, "agent_id": agent_id, "revoked": True}

    @staticmethod
    def _pagination(limit: int, offset: int) -> None:
        if not 1 <= limit <= 1000 or offset < 0:
            raise ValidationError("Invalid pagination")

    def list_files(
        self,
        workspace_id: str,
        actor: Participant | None = None,
        *,
        query: str | None = None,
        limit: int = 100,
        offset: int = 0,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        self._pagination(limit, offset)
        if query is not None and len(query) > 500:
            raise ValidationError("Search is limited to 500 characters")
        where, values = ["m.workspace_id=?", "w.id!='local'"], [workspace_id]
        if query:
            where.append(
                "(instr(casefold(f.path),?)>0 OR instr(casefold(w.name),?)>0 OR instr(casefold(f.job_id),?)>0)"
            )
            values.extend([query.casefold()] * 3)
        if job_id:
            where.append("f.job_id=?")
            values.append(job_id)
        from_sql = (
            " FROM downloaded_files f JOIN workers w ON w.id=f.worker_id JOIN workspace_members m ON m.worker_id=w.id WHERE "
            + " AND ".join(where)
        )
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            self._scope(connection, workspace_id, actor)
            totals = connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(f.size_bytes),0),COALESCE(SUM(f.present=1),0)"
                + from_sql,
                values,
            ).fetchone()
            rows = connection.execute(
                "SELECT f.*,w.name AS worker_name"
                + from_sql
                + " ORDER BY f.downloaded_at DESC,f.job_id DESC LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
        items = [
            {
                key: row[key]
                for key in (
                    "job_id",
                    "worker_id",
                    "worker_name",
                    "path",
                    "size_bytes",
                    "downloaded_at",
                    "checked_at",
                )
            }
            | {"present": bool(row["present"]) if row["present"] is not None else None}
            for row in rows
        ]
        if job_id and not items:
            raise NotFoundError(
                "File metadata was not found on a current workspace worker"
            )
        return {
            "items": items,
            "total": totals[0],
            "total_bytes": totals[1],
            "present_count": totals[2],
            "limit": limit,
            "offset": offset,
        }

    def list_jobs(
        self,
        workspace_id: str,
        actor: Participant | None = None,
        *,
        query: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        self._pagination(limit, offset)
        if query is not None and len(query) > 500:
            raise ValidationError("Search is limited to 500 characters")
        if status not in {
            None,
            "queued",
            "downloading",
            "paused",
            "completed",
            "failed",
            "cancelled",
            "active",
            "attention",
        }:
            raise ValidationError("Invalid job status")
        clauses, values = ["workspace_id=?"], [workspace_id]
        if status in {"active", "attention"}:
            clauses.append("status IN (?,?)")
            values.extend(
                ("queued", "downloading")
                if status == "active"
                else ("paused", "failed")
            )
        elif status:
            clauses.append("status=?")
            values.append(status)
        if query:
            clauses.append(
                "instr(casefold(COALESCE(title,'') || ' ' || source_url || ' ' || canonical_url || ' ' || job_id),?)>0"
            )
            values.append(query.casefold())
        where = " WHERE " + " AND ".join(clauses)
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            self._scope(connection, workspace_id, actor)
            total = connection.execute(
                "SELECT COUNT(*) FROM workspace_jobs" + where, values
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT * FROM workspace_jobs"
                + where
                + " ORDER BY created_at DESC,job_id DESC LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
        return {
            "items": [self._linked_job(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @staticmethod
    def _linked_job(row: sqlite3.Row) -> dict[str, Any]:
        return json.loads(row["job_json"]) | {
            "workspace_id": row["workspace_id"],
            "request_id": row["request_id"],
            "queue_removed": bool(row["queue_removed"]),
        }

    def _request_result(
        self, connection: sqlite3.Connection, request: sqlite3.Row, replayed: bool
    ) -> dict[str, Any]:
        total = connection.execute(
            "SELECT COUNT(*) FROM workspace_jobs WHERE request_id=? AND root_job_id=job_id",
            (request["id"],),
        ).fetchone()[0]
        rows = connection.execute(
            "SELECT * FROM workspace_jobs WHERE request_id=? AND root_job_id=job_id ORDER BY rowid LIMIT 50",
            (request["id"],),
        ).fetchall()
        duplicate_count = connection.execute(
            "SELECT json_array_length(duplicates) FROM workspace_download_requests WHERE id=?",
            (request["id"],),
        ).fetchone()[0]
        duplicates = [
            row[0]
            for row in connection.execute(
                "SELECT j.value FROM workspace_download_requests r,json_each(r.duplicates) j "
                "WHERE r.id=? ORDER BY j.key LIMIT 50",
                (request["id"],),
            ).fetchall()
        ]
        return {
            "request_id": request["id"],
            "workspace_id": request["workspace_id"],
            "jobs": [self._linked_job(row) for row in rows],
            "dispatched": total,
            "jobs_total": total,
            "jobs_truncated": total > len(rows),
            "duplicates": duplicates,
            "duplicate_count": duplicate_count,
            "duplicates_truncated": duplicate_count > len(duplicates),
            "replayed": replayed,
        }

    def downloads(
        self, workspace_id: str, actor: Participant | None, payload: dict[str, Any]
    ) -> dict[str, Any]:
        request = _model(DownloadRequest, payload)
        output_subdir = validate_output_subdir(request.output_subdir)
        normalized, duplicates, seen = [], [], set()
        for index, url in enumerate(request.urls):
            try:
                canonical, identity = normalize_download_url(url)
            except ValidationError as exc:
                raise ValidationError(f"URL {index}: {exc}") from exc
            if request.format != "original" and canonical.lower().startswith("s3://"):
                raise ValidationError(
                    "S3 objects preserve their original format; choose original"
                )
            if identity in seen:
                duplicates.append(url.strip())
            else:
                seen.add(identity)
                normalized.append((url.strip(), canonical, identity))
        target = request.target_worker_id or "auto"
        if target == "local":
            raise ValidationError(
                "The controller cannot download; select a workspace worker"
            )
        actor_id, actor_type = (actor.id, actor.type) if actor else ("admin", "admin")
        request_hash = _hash(
            _json(
                {
                    "urls": request.urls,
                    "format": request.format,
                    "target_worker_id": target,
                    "output_subdir": output_subdir,
                }
            )
        )
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._scope(connection, workspace_id, actor)
            if request.idempotency_key:
                previous = connection.execute(
                    "SELECT id,workspace_id,request_hash FROM workspace_download_requests "
                    "WHERE workspace_id=? AND actor_type=? AND actor_id=? AND idempotency_key=?",
                    (workspace_id, actor_type, actor_id, request.idempotency_key),
                ).fetchone()
                if previous:
                    if previous["request_hash"] != request_hash:
                        raise ConflictError(
                            "Download idempotency key was already used for a different request"
                        )
                    result = self._request_result(connection, previous, True)
                    connection.commit()
                    return result
            workers = connection.execute(
                "SELECT w.* FROM workers w JOIN workspace_members m ON m.worker_id=w.id "
                "WHERE m.workspace_id=? AND w.id!='local' ORDER BY w.id",
                (workspace_id,),
            ).fetchall()
            if target != "auto":
                worker = next((row for row in workers if row["id"] == target), None)
                if not worker:
                    raise AccessDenied(
                        "Selected worker is not enrolled in this workspace"
                    )
                if request.format not in json.loads(worker["capabilities"]):
                    raise ConflictError(
                        "Selected workspace worker does not support this format"
                    )
                slots = [worker["id"]]
            else:
                threshold, now = self._worker_threshold(connection), utc_now()
                slots = [
                    row["id"]
                    for row in workers
                    if row["last_seen_at"] >= threshold
                    and (not row["cooldown_until"] or row["cooldown_until"] <= now)
                    and request.format in json.loads(row["capabilities"])
                    for _ in range(min(64, max(1, row["max_concurrency"])))
                ]
                if not slots:
                    raise ConflictError(
                        "Enroll an online workspace worker that supports this format"
                    )
            request_id, batch_id, now = str(uuid.uuid4()), str(uuid.uuid4()), utc_now()
            options = {
                "format": request.format,
                "target_worker_id": target,
                "output_subdir": output_subdir,
            }
            connection.execute(
                "INSERT INTO workspace_download_requests(id,workspace_id,actor_id,actor_type,idempotency_key,request_hash,batch_id,options,duplicates,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    workspace_id,
                    actor_id,
                    actor_type,
                    request.idempotency_key,
                    request_hash,
                    batch_id,
                    _json(options),
                    _json(duplicates),
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO batches(id,idempotency_key,format,target_worker_id,duplicates,created_at) VALUES(?,?,?,?,?,?)",
                (
                    batch_id,
                    "workspace-request:" + request_id,
                    request.format,
                    None if target == "auto" else target,
                    _json(duplicates),
                    now,
                ),
            )
            initial = []
            for index, (source, canonical, identity) in enumerate(normalized):
                job_id, worker_id = str(uuid.uuid4()), slots[index % len(slots)]
                kind = "discover" if is_s3_listing(canonical) else "download"
                connection.execute(
                    "INSERT INTO jobs(id,batch_id,source_url,canonical_url,dedupe_key,format,status,stage,target_worker_id,"
                    "max_attempts,output_subdir,next_attempt_at,created_at,updated_at,kind,task_options,root_job_id) "
                    "VALUES(?,?,?,?,?,?,'queued','queued',?,3,?,?,?,?,?,?,?)",
                    (
                        job_id,
                        batch_id,
                        source,
                        canonical,
                        identity,
                        request.format,
                        worker_id,
                        output_subdir,
                        now,
                        now,
                        now,
                        kind,
                        _json({"limit": 5000} if kind == "discover" else {}),
                        job_id,
                    ),
                )
                initial.append((job_id, canonical, identity, worker_id, kind))
            # Preserve exact objects submitted alongside a covering prefix even
            # when their completed queue row is cleared before listing arrives.
            roots = [
                (job_id, parse_s3_url(canonical))
                for job_id, canonical, _, _, kind in initial
                if kind == "discover"
            ]
            for job_id, canonical, identity, worker_id, kind in initial:
                if kind != "download" or not canonical.startswith("s3://"):
                    continue
                location = parse_s3_url(canonical)
                for root_id, source in roots:
                    if location.bucket == source.bucket and location.key.startswith(
                        source.key
                    ):
                        connection.execute(
                            "INSERT OR IGNORE INTO discovered_objects(root_job_id,dedupe_key,child_job_id,worker_id,url,size_bytes,title,created_at) VALUES(?,?,?,?,?,NULL,NULL,?)",
                            (root_id, identity, job_id, worker_id, canonical, now),
                        )
            self.database._event(
                connection,
                "workspace.downloads",
                {
                    "workspace_id": workspace_id,
                    "request_id": request_id,
                    "count": len(initial),
                },
            )
            saved = connection.execute(
                "SELECT id,workspace_id FROM workspace_download_requests WHERE id=?",
                (request_id,),
            ).fetchone()
            result = self._request_result(connection, saved, False)
            connection.commit()
            return result

    @staticmethod
    def _message(row: sqlite3.Row) -> dict[str, Any]:
        return {
            key: row[key]
            for key in (
                "seq",
                "id",
                "workspace_id",
                "sender_id",
                "sender_type",
                "sender_name",
                "recipient_id",
                "recipient_type",
                "text",
                "created_at",
            )
        }

    def _recipient(
        self, connection: sqlite3.Connection, workspace_id: str, recipient_id: str
    ) -> str:
        worker = connection.execute(
            "SELECT 1 FROM workspace_members WHERE workspace_id=? AND worker_id=?",
            (workspace_id, recipient_id),
        ).fetchone()
        agent = connection.execute(
            "SELECT 1 FROM workspace_agents WHERE workspace_id=? AND id=? AND revoked_at IS NULL",
            (workspace_id, recipient_id),
        ).fetchone()
        if worker and agent:
            raise ConflictError("Recipient identity is ambiguous")
        if not worker and not agent:
            raise NotFoundError("Recipient is not a current member of this workspace")
        return "worker" if worker else "agent"

    def send_message(
        self, workspace_id: str, actor: Participant | None, payload: dict[str, Any]
    ) -> dict[str, Any]:
        message = _model(MessageCreate, payload)
        if not message.text.strip():
            raise ValidationError("Message text is empty")
        actor_id, actor_type, actor_name = (
            (actor.id, actor.type, actor.name)
            if actor
            else ("admin", "admin", "Administrator")
        )
        request_hash = _hash(
            _json({"text": message.text, "recipient_id": message.recipient_id})
        )
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._scope(connection, workspace_id, actor)
            if message.idempotency_key:
                previous = connection.execute(
                    "SELECT * FROM workspace_messages WHERE workspace_id=? AND sender_type=? AND sender_id=? AND idempotency_key=?",
                    (workspace_id, actor_type, actor_id, message.idempotency_key),
                ).fetchone()
                if previous:
                    if previous["request_hash"] != request_hash:
                        raise ConflictError(
                            "Message idempotency key was already used for different text or recipient"
                        )
                    connection.commit()
                    return self._message(previous)
            recipient_type = (
                self._recipient(connection, workspace_id, message.recipient_id)
                if message.recipient_id
                else None
            )
            message_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO workspace_messages(id,workspace_id,sender_id,sender_type,sender_name,recipient_id,recipient_type,text,idempotency_key,request_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    message_id,
                    workspace_id,
                    actor_id,
                    actor_type,
                    actor_name,
                    message.recipient_id,
                    recipient_type,
                    message.text,
                    message.idempotency_key,
                    request_hash,
                    utc_now(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM workspace_messages WHERE id=?", (message_id,)
            ).fetchone()
            connection.commit()
            return self._message(row)

    def list_messages(
        self,
        workspace_id: str,
        actor: Participant | None = None,
        *,
        after_seq: int = 0,
        limit: int = 100,
        recipient_id: str | None = None,
    ) -> dict[str, Any]:
        self._pagination(limit, after_seq)
        if recipient_id is not None and len(recipient_id) > 200:
            raise ValidationError("Invalid recipient filter")
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            self._scope(connection, workspace_id, actor)
            clauses, values = ["workspace_id=?", "seq>?"], [workspace_id, after_seq]
            if actor is not None:
                clauses.append(
                    "(recipient_id IS NULL OR (sender_id=? AND sender_type=?) "
                    "OR (recipient_id=? AND recipient_type=?))"
                )
                values.extend([actor.id, actor.type, actor.id, actor.type])
            if recipient_id is not None:
                clauses.append("(recipient_id=? OR sender_id=?)")
                values.extend([recipient_id, recipient_id])
            rows = connection.execute(
                "SELECT * FROM workspace_messages WHERE "
                + " AND ".join(clauses)
                + " ORDER BY seq LIMIT ?",
                [*values, limit + 1],
            ).fetchall()
            more = len(rows) > limit
            rows = rows[:limit]
            items = [self._message(row) for row in rows]
            if more:
                # Stop at the last returned visible row. Advancing to a hidden
                # row past another visible message would lose that message.
                next_after = rows[-1]["seq"]
            else:
                latest = connection.execute(
                    "SELECT COALESCE(MAX(seq),0) FROM workspace_messages WHERE workspace_id=?",
                    (workspace_id,),
                ).fetchone()[0]
                next_after = max(after_seq, latest)
        return {"items": items, "next_after_seq": next_after, "has_more": more}


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, AccessDenied):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=401, detail=str(exc))
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


def register_workspace_routes(
    app: FastAPI, control: APIRouter, database: Database
) -> None:
    store = WorkspaceStore(database)
    app.state.workspace_store = store

    def invoke(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (ValidationError, NotFoundError, ConflictError, PermissionError) as exc:
            raise _http_error(exc) from exc

    def participant_auth(
        authorization: Annotated[str | None, Header()] = None,
        worker_id: Annotated[str | None, Header(alias="X-Ateable-Worker-ID")] = None,
        agent_id: Annotated[str | None, Header(alias="X-Ateable-Agent-ID")] = None,
    ) -> Participant:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=401, detail="Participant bearer token required"
            )
        return invoke(
            store.authenticate,
            authorization.removeprefix("Bearer ").strip(),
            worker_id=worker_id,
            agent_id=agent_id,
        )

    participant = APIRouter(prefix="/api/participant")
    Actor = Annotated[Participant, Depends(participant_auth)]

    @control.get("/workspaces")
    def workspaces():
        return store.list_workspaces()

    @control.post("/workspaces", status_code=201)
    def create_workspace(payload: WorkspaceCreate):
        return invoke(store.create_workspace, payload.name, payload.description)

    @control.get("/workspaces/{workspace_id}")
    def get_workspace(workspace_id: str):
        return invoke(store.get_workspace, workspace_id)

    @control.patch("/workspaces/{workspace_id}")
    def patch_workspace(workspace_id: str, payload: WorkspacePatch):
        return invoke(
            store.update_workspace,
            workspace_id,
            **payload.model_dump(exclude_unset=True),
        )

    @control.get("/workspaces/{workspace_id}/members")
    def members(workspace_id: str):
        return invoke(store.list_members, workspace_id)

    @control.post("/workspaces/{workspace_id}/members", status_code=201)
    def add_member(workspace_id: str, payload: MemberCreate):
        return invoke(store.add_member, workspace_id, payload.worker_id)

    @control.delete("/workspaces/{workspace_id}/members/{worker_id}")
    def remove_member(workspace_id: str, worker_id: str):
        return invoke(store.remove_member, workspace_id, worker_id)

    @control.get("/workspaces/{workspace_id}/agents")
    def agents(workspace_id: str):
        return invoke(store.list_agents, workspace_id)

    @control.post("/workspaces/{workspace_id}/agents", status_code=201)
    def create_agent(workspace_id: str, payload: AgentCreate):
        return invoke(store.create_agent, workspace_id, payload.name)

    @control.delete("/workspaces/{workspace_id}/agents/{agent_id}")
    def revoke_agent(workspace_id: str, agent_id: str):
        return invoke(store.revoke_agent, workspace_id, agent_id)

    @participant.get("/workspaces")
    def participant_workspaces(actor: Actor):
        return invoke(store.list_workspaces, actor)

    @participant.get("/workspaces/{workspace_id}/members")
    def participant_members(workspace_id: str, actor: Actor):
        return invoke(store.list_members, workspace_id, actor)

    @participant.get("/workspaces/{workspace_id}/files")
    def participant_files(
        workspace_id: str,
        actor: Actor,
        query: Annotated[str | None, Query(alias="q", max_length=500)] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        return invoke(
            store.list_files,
            workspace_id,
            actor,
            query=query,
            limit=limit,
            offset=offset,
        )

    @control.get("/workspaces/{workspace_id}/files")
    def admin_files(
        workspace_id: str,
        query: Annotated[str | None, Query(alias="q", max_length=500)] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        return invoke(
            store.list_files, workspace_id, query=query, limit=limit, offset=offset
        )

    @participant.get("/workspaces/{workspace_id}/files/{job_id}")
    def participant_file(workspace_id: str, job_id: str, actor: Actor):
        return invoke(store.list_files, workspace_id, actor, job_id=job_id)["items"][0]

    @participant.get("/workspaces/{workspace_id}/jobs")
    def participant_jobs(
        workspace_id: str,
        actor: Actor,
        query: Annotated[str | None, Query(alias="q", max_length=500)] = None,
        status: str | None = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        return invoke(
            store.list_jobs,
            workspace_id,
            actor,
            query=query,
            status=status,
            limit=limit,
            offset=offset,
        )

    @control.get("/workspaces/{workspace_id}/jobs")
    def admin_jobs(
        workspace_id: str,
        query: Annotated[str | None, Query(alias="q", max_length=500)] = None,
        status: str | None = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        return invoke(
            store.list_jobs,
            workspace_id,
            query=query,
            status=status,
            limit=limit,
            offset=offset,
        )

    @participant.post("/workspaces/{workspace_id}/downloads", status_code=201)
    def participant_downloads(workspace_id: str, actor: Actor, payload: dict[str, Any]):
        return invoke(store.downloads, workspace_id, actor, payload)

    @control.post("/workspaces/{workspace_id}/downloads", status_code=201)
    def admin_downloads(workspace_id: str, payload: dict[str, Any]):
        return invoke(store.downloads, workspace_id, None, payload)

    @participant.get("/workspaces/{workspace_id}/messages")
    def participant_messages(
        workspace_id: str,
        actor: Actor,
        after_seq: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        recipient_id: Annotated[str | None, Query(max_length=200)] = None,
    ):
        return invoke(
            store.list_messages,
            workspace_id,
            actor,
            after_seq=after_seq,
            limit=limit,
            recipient_id=recipient_id,
        )

    @control.get("/workspaces/{workspace_id}/messages")
    def admin_messages(
        workspace_id: str,
        after_seq: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        recipient_id: Annotated[str | None, Query(max_length=200)] = None,
    ):
        return invoke(
            store.list_messages,
            workspace_id,
            after_seq=after_seq,
            limit=limit,
            recipient_id=recipient_id,
        )

    @participant.post("/workspaces/{workspace_id}/messages", status_code=201)
    def participant_send(workspace_id: str, actor: Actor, payload: dict[str, Any]):
        return invoke(store.send_message, workspace_id, actor, payload)

    @control.post("/workspaces/{workspace_id}/messages", status_code=201)
    def admin_send(workspace_id: str, payload: dict[str, Any]):
        return invoke(store.send_message, workspace_id, None, payload)

    app.include_router(participant)
