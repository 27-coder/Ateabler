"""Durable, offline link collections and narrowly scoped browser capture.

Capturing never performs network I/O. Dispatch reservations and queue insertion
are joined by a private idempotency key and SQLite triggers, so even a process
exit after queue commit cannot create duplicate downloads on recovery.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import uuid
from collections import defaultdict
from threading import RLock
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, FastAPI, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as ModelError

from .database import ConflictError, Database, NotFoundError, utc_now
from .schemas import MediaFormat
from .validation import ValidationError, normalize_download_url, validate_output_subdir


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CaptureItem(StrictModel):
    url: str = Field(min_length=1, max_length=8192)
    title: str | None = Field(default=None, max_length=512)
    source_page: str | None = Field(default=None, max_length=8192)


class CaptureRequest(StrictModel):
    items: list[CaptureItem] = Field(min_length=1, max_length=5000)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)
    origin: str | None = Field(default=None, max_length=128)


class BucketCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)


class BucketPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)


class DispatchRequest(StrictModel):
    format: MediaFormat = "original"
    target_worker_id: str | None = Field(default="auto", max_length=200)
    output_subdir: str | None = Field(default=None, max_length=240)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)
    limit: int = Field(default=5000, ge=1, le=5000)
    item_ids: list[str] | None = Field(default=None, min_length=1, max_length=5000)
    retry_existing: bool = False
    max_attempts: int = Field(default=3, ge=1, le=10)


class CaptureTokenCreate(StrictModel):
    bucket_id: str = Field(min_length=1, max_length=200)
    name: str = Field(default="Browser capture", max_length=120)


def _model(model: type[BaseModel], payload: Any) -> BaseModel:
    """Return concise validation errors without reflecting URLs or secrets."""
    try:
        return model.model_validate(payload)
    except ModelError as exc:
        errors = [
            {"loc": error["loc"], "message": error["msg"]}
            for error in exc.errors(include_input=False, include_context=False)
        ]
        raise ValidationError(json.dumps(errors, ensure_ascii=False)) from exc


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class BucketStore:
    """Repository usable both by the web API and an offline PC handler."""

    def __init__(self, database: Database):
        self.database = database
        self._dispatch_lock = RLock()
        self.initialize()

    def initialize(self) -> None:
        with self.database.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS buckets (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT UNIQUE NOT NULL,
                    description TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bucket_dispatch_runs (
                    id TEXT PRIMARY KEY, bucket_id TEXT NOT NULL,
                    idempotency_key TEXT, status TEXT NOT NULL,
                    options TEXT NOT NULL, batch_id TEXT, error TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(bucket_id,idempotency_key),
                    FOREIGN KEY(bucket_id) REFERENCES buckets(id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_bucket_active_run
                    ON bucket_dispatch_runs(bucket_id) WHERE status='prepared';
                CREATE TABLE IF NOT EXISTS bucket_items (
                    id TEXT PRIMARY KEY, bucket_id TEXT NOT NULL, source_url TEXT NOT NULL,
                    canonical_url TEXT NOT NULL, dedupe_key TEXT NOT NULL,
                    title TEXT, source_page TEXT, origin TEXT, kind TEXT NOT NULL,
                    dispatch_run_id TEXT, removed_at TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    FOREIGN KEY(bucket_id) REFERENCES buckets(id),
                    FOREIGN KEY(dispatch_run_id) REFERENCES bucket_dispatch_runs(id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_bucket_item_dedupe
                    ON bucket_items(bucket_id,dedupe_key) WHERE removed_at IS NULL;
                CREATE INDEX IF NOT EXISTS idx_bucket_items_list
                    ON bucket_items(bucket_id,removed_at,created_at,id);
                CREATE TABLE IF NOT EXISTS bucket_dispatch_items (
                    run_id TEXT NOT NULL, item_id TEXT NOT NULL,
                    source_url TEXT NOT NULL, canonical_url TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL, job_id TEXT,
                    batch_id TEXT, job_status TEXT, job_stage TEXT, job_title TEXT,
                    job_format TEXT, job_target_worker_id TEXT, job_claimed_by TEXT,
                    job_progress REAL, job_attempts INTEGER, job_max_attempts INTEGER,
                    job_output_path TEXT, job_error TEXT,
                    job_created_at TEXT, job_updated_at TEXT,
                    PRIMARY KEY(run_id,item_id),
                    FOREIGN KEY(run_id) REFERENCES bucket_dispatch_runs(id),
                    FOREIGN KEY(item_id) REFERENCES bucket_items(id)
                );
                CREATE INDEX IF NOT EXISTS idx_bucket_link_job ON bucket_dispatch_items(job_id);
                CREATE TABLE IF NOT EXISTS bucket_capture_receipts (
                    bucket_id TEXT NOT NULL, scope TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                    response TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(bucket_id,scope,idempotency_key),
                    FOREIGN KEY(bucket_id) REFERENCES buckets(id)
                );
                CREATE TABLE IF NOT EXISTS bucket_capture_tokens (
                    id TEXT PRIMARY KEY, bucket_id TEXT NOT NULL, name TEXT NOT NULL,
                    token_hash TEXT UNIQUE NOT NULL, revoked_at TEXT,
                    created_at TEXT NOT NULL, last_used_at TEXT,
                    FOREIGN KEY(bucket_id) REFERENCES buckets(id)
                );
                """
            )
            # These triggers participate in the queue's own transaction. The
            # history survives queue cleanup and the enqueue/finalize crash gap.
            assignment = ", ".join(
                f"job_{column}=NEW.{column}"
                for column in (
                    "status",
                    "stage",
                    "title",
                    "format",
                    "target_worker_id",
                    "claimed_by",
                    "progress",
                    "attempts",
                    "max_attempts",
                    "output_path",
                    "error",
                    "created_at",
                    "updated_at",
                )
            )
            deletion_assignment = assignment.replace("NEW.", "OLD.")
            connection.executescript(
                f"""
                BEGIN IMMEDIATE;
                DROP TRIGGER IF EXISTS bucket_job_insert;
                CREATE TRIGGER bucket_job_insert AFTER INSERT ON jobs
                BEGIN
                    UPDATE bucket_dispatch_items SET job_id=NEW.id, batch_id=NEW.batch_id,
                        {assignment}
                    WHERE job_id IS NULL AND dedupe_key=NEW.dedupe_key AND run_id IN (
                        SELECT r.id FROM bucket_dispatch_runs r JOIN batches b
                        ON b.id=NEW.batch_id AND b.idempotency_key='bucket-run:' || r.id
                    );
                    UPDATE bucket_dispatch_runs SET batch_id=NEW.batch_id
                    WHERE id IN (SELECT run_id FROM bucket_dispatch_items WHERE job_id=NEW.id);
                END;
                CREATE TRIGGER IF NOT EXISTS bucket_job_update AFTER UPDATE ON jobs
                BEGIN
                    UPDATE bucket_dispatch_items SET {assignment} WHERE job_id=NEW.id;
                END;
                CREATE TRIGGER IF NOT EXISTS bucket_job_delete BEFORE DELETE ON jobs
                BEGIN
                    UPDATE bucket_dispatch_items SET {deletion_assignment} WHERE job_id=OLD.id;
                END;
                COMMIT;
                """
            )
            now = utc_now()
            connection.execute(
                "INSERT OR IGNORE INTO buckets(id,name,name_key,created_at,updated_at) "
                "VALUES ('inbox','Inbox','inbox',?,?)",
                (now, now),
            )

    @staticmethod
    def _require_bucket(connection: sqlite3.Connection, bucket_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM buckets WHERE id=?", (bucket_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("Bucket was not found")
        return row

    def create_bucket(self, name: str, description: str = "") -> dict[str, Any]:
        payload = _model(BucketCreate, {"name": name, "description": description})
        name = payload.name.strip()
        if not name:
            raise ValidationError("Bucket name is empty")
        bucket_id, now = str(uuid.uuid4()), utc_now()
        try:
            with self.database.connect() as connection:
                connection.execute(
                    "INSERT INTO buckets(id,name,name_key,description,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (bucket_id, name, name.casefold(), description, now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("A bucket with this name already exists") from exc
        return self.get_bucket(bucket_id)

    def update_bucket(
        self, bucket_id: str, *, name: str | None = None, description: str | None = None
    ) -> dict[str, Any]:
        _model(BucketPatch, {"name": name, "description": description})
        if name is not None:
            name = name.strip()
            if not name:
                raise ValidationError("Bucket name is empty")
        try:
            with self.database.connect() as connection:
                self._require_bucket(connection, bucket_id)
                connection.execute(
                    "UPDATE buckets SET name=COALESCE(?,name),name_key=COALESCE(?,name_key),"
                    "description=COALESCE(?,description),updated_at=? WHERE id=?",
                    (
                        name,
                        name.casefold() if name else None,
                        description,
                        utc_now(),
                        bucket_id,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("A bucket with this name already exists") from exc
        return self.get_bucket(bucket_id)

    def list_buckets(self) -> dict[str, Any]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM buckets ORDER BY created_at,id"
            ).fetchall()
            items = [self._bucket(connection, row) for row in rows]
        return {"items": items, "total": len(items)}

    def get_bucket(self, bucket_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            return self._bucket(connection, self._require_bucket(connection, bucket_id))

    @staticmethod
    def _bucket(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        states = connection.execute(
            "SELECT CASE WHEN d.job_status='completed' AND d.job_stage='expanded' THEN 'expanded' "
            "WHEN d.job_status IS NOT NULL THEN d.job_status "
            "WHEN i.dispatch_run_id IS NOT NULL THEN 'dispatching' ELSE 'pending' END AS state,"
            "COUNT(*) AS count FROM bucket_items i LEFT JOIN bucket_dispatch_items d "
            "ON d.item_id=i.id AND d.run_id=i.dispatch_run_id "
            "WHERE i.bucket_id=? AND i.removed_at IS NULL GROUP BY state",
            (row["id"],),
        ).fetchall()
        counts = {entry["state"]: entry["count"] for entry in states}
        total = sum(counts.values())
        active = connection.execute(
            "SELECT id,status,options,error,created_at,updated_at FROM bucket_dispatch_runs "
            "WHERE bucket_id=? AND status='prepared'",
            (row["id"],),
        ).fetchone()
        active_dispatch = None
        if active:
            active_dispatch = {
                "run_id": active["id"],
                "status": active["status"],
                "options": json.loads(active["options"]),
                "error": active["error"],
                "created_at": active["created_at"],
                "updated_at": active["updated_at"],
            }
        return {
            **{
                key: row[key]
                for key in ("id", "name", "description", "created_at", "updated_at")
            },
            "total": total,
            "pending": counts.get("pending", 0),
            "dispatched": total
            - counts.get("pending", 0)
            - counts.get("dispatching", 0),
            "counts": counts,
            "active_dispatch": active_dispatch,
        }

    def summary(self) -> dict[str, Any]:
        buckets = self.list_buckets()["items"]
        counts: dict[str, int] = defaultdict(int)
        for bucket in buckets:
            for key, count in bucket["counts"].items():
                counts[key] += count
        return {
            "buckets": len(buckets),
            "total": sum(bucket["total"] for bucket in buckets),
            "pending": counts.get("pending", 0),
            "counts": dict(counts),
            "dispatching": counts.get("dispatching", 0),
            "active_dispatches": [
                {"bucket_id": bucket["id"], **bucket["active_dispatch"]}
                for bucket in buckets
                if bucket["active_dispatch"]
            ],
        }

    @staticmethod
    def _validated_capture(
        items: list[dict[str, Any]], origin: str | None, idempotency_key: str | None
    ) -> tuple[CaptureRequest, list[dict[str, Any]]]:
        payload = _model(
            CaptureRequest,
            {
                "items": items,
                "origin": origin,
                "idempotency_key": idempotency_key,
            },
        )
        normalized = []
        for index, item in enumerate(payload.items):
            try:
                canonical_url, dedupe_key = normalize_download_url(item.url)
                if item.source_page:
                    # Metadata only: validate it locally, without resolving it.
                    normalize_download_url(item.source_page)
            except ValidationError as exc:
                raise ValidationError(f"Item {index}: {exc}") from exc
            scheme = urlparse(canonical_url).scheme.lower()
            kind = "url"
            if scheme == "s3":
                path = urlparse(canonical_url).path
                kind = "s3_prefix" if not path or path.endswith("/") else "s3_object"
            normalized.append(
                {
                    "source_url": item.url.strip(),
                    "canonical_url": canonical_url,
                    "dedupe_key": dedupe_key,
                    "title": item.title,
                    "source_page": item.source_page,
                    "kind": kind,
                }
            )
        return payload, normalized

    def capture(
        self,
        bucket_id: str,
        items: list[dict[str, Any]],
        *,
        origin: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload, normalized = self._validated_capture(items, origin, idempotency_key)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_bucket(connection, bucket_id)
            return self._capture(connection, bucket_id, normalized, payload, "admin")

    def _capture(
        self,
        connection: sqlite3.Connection,
        bucket_id: str,
        normalized: list[dict[str, Any]],
        payload: CaptureRequest,
        scope: str,
    ) -> dict[str, Any]:
        if payload.idempotency_key:
            receipt = connection.execute(
                "SELECT response FROM bucket_capture_receipts WHERE bucket_id=? AND scope=? "
                "AND idempotency_key=?",
                (bucket_id, scope, payload.idempotency_key),
            ).fetchone()
            if receipt:
                connection.commit()
                return json.loads(receipt["response"])
        now, accepted, duplicates = utc_now(), [], 0
        for item in normalized:
            existing = connection.execute(
                "SELECT id FROM bucket_items WHERE bucket_id=? AND dedupe_key=? AND removed_at IS NULL",
                (bucket_id, item["dedupe_key"]),
            ).fetchone()
            if existing:
                duplicates += 1
                continue
            item_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO bucket_items(id,bucket_id,source_url,canonical_url,dedupe_key,title,"
                "source_page,origin,kind,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item_id,
                    bucket_id,
                    item["source_url"],
                    item["canonical_url"],
                    item["dedupe_key"],
                    item["title"],
                    item["source_page"],
                    payload.origin,
                    item["kind"],
                    now,
                    now,
                ),
            )
            accepted.append(
                {
                    "id": item_id,
                    "bucket_id": bucket_id,
                    **item,
                    "origin": payload.origin,
                    "created_at": now,
                    "updated_at": now,
                    "status": "pending",
                    "job_id": None,
                    "job_status": None,
                    "history": [],
                }
            )
        response = {
            "bucket_id": bucket_id,
            "accepted": len(accepted),
            "duplicates": duplicates,
            "items": accepted,
        }
        if payload.idempotency_key:
            connection.execute(
                "INSERT INTO bucket_capture_receipts(bucket_id,scope,idempotency_key,response,created_at) "
                "VALUES (?,?,?,?,?)",
                (bucket_id, scope, payload.idempotency_key, _json(response), now),
            )
        connection.execute(
            "UPDATE buckets SET updated_at=? WHERE id=?", (now, bucket_id)
        )
        connection.commit()
        return response

    def list_items(
        self,
        bucket_id: str,
        *,
        query: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 1000 or offset < 0:
            raise ValidationError("Invalid pagination")
        if query is not None and (not isinstance(query, str) or len(query) > 500):
            raise ValidationError("Search is limited to 500 characters")
        valid_states = {
            "pending",
            "dispatching",
            "queued",
            "downloading",
            "paused",
            "completed",
            "failed",
            "cancelled",
            "expanded",
        }
        if status is not None and status not in valid_states:
            raise ValidationError("Invalid item status")
        clauses, values = ["i.bucket_id=?", "i.removed_at IS NULL"], [bucket_id]
        if query:
            clauses.append(
                "(instr(casefold(i.source_url),?)>0 OR instr(casefold(i.title),?)>0 "
                "OR instr(casefold(i.source_page),?)>0)"
            )
            values.extend([query.casefold()] * 3)
        state_sql = (
            "CASE WHEN d.job_status='completed' AND d.job_stage='expanded' THEN 'expanded' "
            "WHEN d.job_status IS NOT NULL THEN d.job_status "
            "WHEN i.dispatch_run_id IS NOT NULL THEN 'dispatching' ELSE 'pending' END"
        )
        if status:
            clauses.append(f"({state_sql})=?")
            values.append(status)
        from_sql = (
            "FROM bucket_items i LEFT JOIN bucket_dispatch_items d ON d.item_id=i.id "
            "AND d.run_id=i.dispatch_run_id WHERE " + " AND ".join(clauses)
        )
        with self.database.connect() as connection:
            self._require_bucket(connection, bucket_id)
            total = connection.execute(
                f"SELECT COUNT(*) {from_sql}", values
            ).fetchone()[0]
            rows = connection.execute(
                f"SELECT i.*,({state_sql}) AS state {from_sql} ORDER BY i.created_at,i.rowid LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
            histories: dict[str, list[dict[str, Any]]] = defaultdict(list)
            if rows:
                ids = [row["id"] for row in rows]
                linked = connection.execute(
                    f"SELECT * FROM bucket_dispatch_items WHERE item_id IN ({','.join('?' for _ in ids)})",
                    ids,
                ).fetchall()
                for link in linked:
                    if link["job_id"]:
                        histories[link["item_id"]].append(self._history(link))
            items = []
            for row in rows:
                history = histories[row["id"]]
                latest = history[-1] if history else {}
                item = {
                    key: row[key]
                    for key in (
                        "id",
                        "bucket_id",
                        "source_url",
                        "canonical_url",
                        "title",
                        "source_page",
                        "origin",
                        "kind",
                        "created_at",
                        "updated_at",
                        "dispatch_run_id",
                    )
                }
                item.update(
                    status=row["state"],
                    job_id=latest.get("job_id"),
                    job_status=latest.get("status"),
                    job_stage=latest.get("stage"),
                    history=history,
                )
                items.append(item)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    @staticmethod
    def _history(link: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_id": link["run_id"],
            "job_id": link["job_id"],
            "batch_id": link["batch_id"],
            **{
                column: link[f"job_{column}"]
                for column in (
                    "status",
                    "stage",
                    "title",
                    "format",
                    "target_worker_id",
                    "claimed_by",
                    "progress",
                    "attempts",
                    "max_attempts",
                    "output_path",
                    "error",
                    "created_at",
                    "updated_at",
                )
            },
        }

    def remove_item(self, bucket_id: str, item_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_bucket(connection, bucket_id)
            item = connection.execute(
                "SELECT * FROM bucket_items WHERE id=? AND bucket_id=? AND removed_at IS NULL",
                (item_id, bucket_id),
            ).fetchone()
            if not item:
                raise NotFoundError("Bucket item was not found")
            if item["dispatch_run_id"]:
                run = connection.execute(
                    "SELECT status FROM bucket_dispatch_runs WHERE id=?",
                    (item["dispatch_run_id"],),
                ).fetchone()
                if run and run["status"] == "prepared":
                    raise ConflictError(
                        "Dispatch is reserved; finish its retry before removing this item"
                    )
            connection.execute(
                "UPDATE bucket_items SET removed_at=?,updated_at=? WHERE id=?",
                (utc_now(), utc_now(), item_id),
            )
            connection.commit()
        return {"removed": True, "item_id": item_id}

    def dispatch(
        self,
        bucket_id: str,
        *,
        media_format: str = "original",
        target_worker_id: str | None = "auto",
        output_subdir: str | None = None,
        idempotency_key: str | None = None,
        limit: int = 5000,
        item_ids: list[str] | None = None,
        retry_existing: bool = False,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        payload = _model(
            DispatchRequest,
            {
                "format": media_format,
                "target_worker_id": target_worker_id,
                "output_subdir": output_subdir,
                "idempotency_key": idempotency_key,
                "limit": limit,
                "item_ids": item_ids,
                "retry_existing": retry_existing,
                "max_attempts": max_attempts,
            },
        )
        output_subdir = validate_output_subdir(payload.output_subdir)
        if target_worker_id == "local":
            raise ConflictError(
                "Downloads run on paired workers; the master cannot download"
            )
        # Reduces needless concurrent recovery in this process; SQLite provides
        # reservation and enqueue idempotency across separate controller processes.
        with self._dispatch_lock:
            run, replayed = self._reserve(bucket_id, payload, output_subdir)
            if run is None:
                return {
                    "run_id": None,
                    "bucket_id": bucket_id,
                    "batch": None,
                    "jobs": [],
                    "dispatched": 0,
                    "pending": self.get_bucket(bucket_id)["pending"],
                    "replayed": False,
                }
            return self._execute_with_error(run, replayed)

    def retry_dispatch(self, bucket_id: str, run_id: str) -> dict[str, Any]:
        """Recover exactly this reservation without selecting new captures."""
        with self._dispatch_lock:
            with self.database.connect() as connection:
                self._require_bucket(connection, bucket_id)
                run = connection.execute(
                    "SELECT * FROM bucket_dispatch_runs WHERE id=? AND bucket_id=?",
                    (run_id, bucket_id),
                ).fetchone()
                if not run:
                    raise NotFoundError("Dispatch run was not found in this bucket")
            return self._execute_with_error(dict(run), True)

    def _execute_with_error(
        self, run: dict[str, Any], replayed: bool
    ) -> dict[str, Any]:
        try:
            return self._execute_run(run, replayed)
        except Exception as exc:
            # Store an operational hint, never a signed URL or credentials
            # from a downstream exception. The reservation remains retryable.
            if isinstance(exc, NotFoundError):
                error = "Selected remote worker was not found; this dispatch remains reserved"
            elif isinstance(exc, ConflictError):
                error = "No eligible remote worker currently accepts this dispatch"
            else:
                error = "Queue write did not finish; retry this reserved dispatch"
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE bucket_dispatch_runs SET error=?,updated_at=? WHERE id=? AND status='prepared'",
                    (
                        error,
                        utc_now(),
                        run["id"],
                    ),
                )
            raise

    def _reserve(
        self, bucket_id: str, payload: DispatchRequest, output_subdir: str | None
    ) -> tuple[dict[str, Any] | None, bool]:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_bucket(connection, bucket_id)
            existing = None
            if payload.idempotency_key:
                existing = connection.execute(
                    "SELECT * FROM bucket_dispatch_runs WHERE bucket_id=? AND idempotency_key=?",
                    (bucket_id, payload.idempotency_key),
                ).fetchone()
            if not existing:
                existing = connection.execute(
                    "SELECT * FROM bucket_dispatch_runs WHERE bucket_id=? AND status='prepared'",
                    (bucket_id,),
                ).fetchone()
            if existing:
                connection.commit()
                return dict(existing), True
            clauses, values = ["i.bucket_id=?", "i.removed_at IS NULL"], [bucket_id]
            if payload.item_ids:
                unique_ids = list(dict.fromkeys(payload.item_ids))
                count = connection.execute(
                    f"SELECT COUNT(*) FROM bucket_items WHERE bucket_id=? AND removed_at IS NULL "
                    f"AND id IN ({','.join('?' for _ in unique_ids)})",
                    [bucket_id, *unique_ids],
                ).fetchone()[0]
                if count != len(unique_ids):
                    raise NotFoundError(
                        "One or more items were not found in this bucket"
                    )
                clauses.append(f"i.id IN ({','.join('?' for _ in unique_ids)})")
                values.extend(unique_ids)
            if payload.retry_existing:
                clauses.append("d.job_status IN ('failed','cancelled')")
                join = " JOIN bucket_dispatch_items d ON d.item_id=i.id AND d.run_id=i.dispatch_run_id "
            else:
                clauses.append("i.dispatch_run_id IS NULL")
                join = ""
            rows = connection.execute(
                "SELECT i.*"
                + (",d.job_id" if payload.retry_existing else "")
                + " FROM bucket_items i"
                + join
                + " WHERE "
                + " AND ".join(clauses)
                + " ORDER BY i.created_at,i.rowid LIMIT ?",
                [*values, payload.limit],
            ).fetchall()
            if not rows:
                connection.commit()
                return None, False
            if not payload.retry_existing:
                # Reject permanent input errors before saving immutable options.
                # Auto placement with no online worker remains recoverable and
                # is intentionally checked later by the queue transaction.
                if payload.format != "original" and any(
                    row["canonical_url"].lower().startswith("s3://") for row in rows
                ):
                    raise ValidationError(
                        "S3 objects preserve their original format; choose original"
                    )
                target = payload.target_worker_id
                if target and target != "auto":
                    worker = connection.execute(
                        "SELECT capabilities FROM workers WHERE id=? AND id!='local'",
                        (target,),
                    ).fetchone()
                    if not worker:
                        raise NotFoundError("Selected remote worker was not found")
                    if payload.format not in json.loads(worker["capabilities"]):
                        raise ConflictError(
                            "Selected remote worker does not support this format"
                        )
            run_id, now = str(uuid.uuid4()), utc_now()
            options = {
                "format": payload.format,
                "target_worker_id": payload.target_worker_id,
                "output_subdir": output_subdir,
                "max_attempts": payload.max_attempts,
                "retry_existing": payload.retry_existing,
            }
            if payload.retry_existing:
                options["job_ids"] = [row["job_id"] for row in rows]
            connection.execute(
                "INSERT INTO bucket_dispatch_runs(id,bucket_id,idempotency_key,status,options,created_at,updated_at) "
                "VALUES (?,?,?,'prepared',?,?,?)",
                (run_id, bucket_id, payload.idempotency_key, _json(options), now, now),
            )
            if not payload.retry_existing:
                for row in rows:
                    connection.execute(
                        "INSERT INTO bucket_dispatch_items(run_id,item_id,source_url,canonical_url,dedupe_key) "
                        "VALUES (?,?,?,?,?)",
                        (
                            run_id,
                            row["id"],
                            row["source_url"],
                            row["canonical_url"],
                            row["dedupe_key"],
                        ),
                    )
                    connection.execute(
                        "UPDATE bucket_items SET dispatch_run_id=?,updated_at=? WHERE id=?",
                        (run_id, now, row["id"]),
                    )
            run = connection.execute(
                "SELECT * FROM bucket_dispatch_runs WHERE id=?", (run_id,)
            ).fetchone()
            connection.commit()
            return dict(run), False

    def _execute_run(self, run: dict[str, Any], replayed: bool) -> dict[str, Any]:
        options = json.loads(run["options"])
        if options["retry_existing"]:
            if run["status"] != "completed":
                # Retrying reuses existing job IDs and never downloads on master.
                self.database.apply_action(
                    action="retry",
                    job_ids=options["job_ids"],
                    status=None,
                    batch_id=None,
                )
            with self.database.connect() as connection:
                jobs = [
                    self.database._job(row)
                    for row in connection.execute(
                        f"SELECT * FROM jobs WHERE id IN ({','.join('?' for _ in options['job_ids'])})",
                        options["job_ids"],
                    ).fetchall()
                ]
            batch = None
        else:
            with self.database.connect() as connection:
                links = connection.execute(
                    "SELECT d.* FROM bucket_dispatch_items d JOIN bucket_items i ON i.id=d.item_id "
                    "WHERE d.run_id=? ORDER BY i.created_at,i.rowid",
                    (run["id"],),
                ).fetchall()
            # Triggered links are already committed if enqueue succeeded before
            # an exception/process exit. Do not enqueue again after queue cleanup.
            if any(not link["job_id"] for link in links):
                batch, jobs, _, _ = self.database.enqueue_batch(
                    urls=[
                        (link["source_url"], link["canonical_url"], link["dedupe_key"])
                        for link in links
                    ],
                    media_format=options["format"],
                    target_worker_id=options["target_worker_id"],
                    output_subdir=options["output_subdir"],
                    proxy=None,
                    cookies_file=None,
                    idempotency_key="bucket-run:" + run["id"],
                    max_attempts=options["max_attempts"],
                    duplicates=[],
                )
            else:
                batch, jobs = self._saved_run_result(run["id"])
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE bucket_dispatch_runs SET status='completed',error=NULL,updated_at=? WHERE id=?",
                (utc_now(), run["id"]),
            )
        return {
            "run_id": run["id"],
            "bucket_id": run["bucket_id"],
            "batch": batch,
            "jobs": jobs,
            "dispatched": len(jobs),
            "pending": self.get_bucket(run["bucket_id"])["pending"],
            "replayed": replayed,
            "options": options,
        }

    def _saved_run_result(
        self, run_id: str
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        with self.database.connect() as connection:
            links = connection.execute(
                "SELECT d.* FROM bucket_dispatch_items d JOIN bucket_items i ON i.id=d.item_id "
                "WHERE d.run_id=? ORDER BY i.created_at,i.rowid",
                (run_id,),
            ).fetchall()
            jobs = []
            batch = None
            for link in links:
                current = connection.execute(
                    "SELECT * FROM jobs WHERE id=?", (link["job_id"],)
                ).fetchone()
                if current:
                    jobs.append(self.database._job(current))
                else:
                    saved = self._history(link)
                    saved["id"] = saved.pop("job_id")
                    saved.update(
                        source_url=link["source_url"],
                        canonical_url=link["canonical_url"],
                        queue_removed=True,
                    )
                    jobs.append(saved)
            if links and links[0]["batch_id"]:
                exists = connection.execute(
                    "SELECT 1 FROM batches WHERE id=?", (links[0]["batch_id"],)
                ).fetchone()
                if exists:
                    batch = self.database._batch(connection, links[0]["batch_id"])
        return batch, jobs

    def export(self, bucket_id: str, *, format: str = "text") -> str | dict[str, Any]:
        bucket = self.get_bucket(bucket_id)
        items = []
        offset = 0
        while True:
            page = self.list_items(bucket_id, limit=1000, offset=offset)
            items.extend(page["items"])
            offset += len(page["items"])
            if offset >= page["total"]:
                break
        if format == "text":
            return "".join(item["source_url"] + "\n" for item in items)
        if format == "json":
            return {"bucket": bucket, "items": items}
        raise ValidationError("Export format must be text or json")

    def create_capture_token(
        self, bucket_id: str, name: str = "Browser capture"
    ) -> dict[str, Any]:
        _model(CaptureTokenCreate, {"bucket_id": bucket_id, "name": name})
        token_id, token, now = (
            str(uuid.uuid4()),
            "acap_" + secrets.token_urlsafe(36),
            utc_now(),
        )
        with self.database.connect() as connection:
            self._require_bucket(connection, bucket_id)
            connection.execute(
                "INSERT INTO bucket_capture_tokens(id,bucket_id,name,token_hash,created_at) VALUES (?,?,?,?,?)",
                (token_id, bucket_id, name, _token_hash(token), now),
            )
        return {
            "token_id": token_id,
            "bucket_id": bucket_id,
            "name": name,
            "token": token,
            "created_at": now,
        }

    def list_capture_tokens(self) -> dict[str, Any]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id AS token_id,bucket_id,name,created_at,last_used_at,revoked_at "
                "FROM bucket_capture_tokens ORDER BY created_at,id",
            ).fetchall()
        return {"items": [dict(row) for row in rows]}

    def revoke_capture_token(self, token_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            changed = connection.execute(
                "UPDATE bucket_capture_tokens SET revoked_at=COALESCE(revoked_at,?) WHERE id=?",
                (utc_now(), token_id),
            ).rowcount
            if not changed:
                raise NotFoundError("Capture token was not found")
        return {"revoked": True, "token_id": token_id}

    @staticmethod
    def _capture_auth(connection: sqlite3.Connection, token: str | None) -> sqlite3.Row:
        if not token or len(token) > 256:
            raise PermissionError("Invalid capture token")
        digest = _token_hash(token)
        row = connection.execute(
            "SELECT * FROM bucket_capture_tokens WHERE token_hash=? AND revoked_at IS NULL",
            (digest,),
        ).fetchone()
        if not row or not hmac.compare_digest(row["token_hash"], digest):
            raise PermissionError("Invalid capture token")
        return row

    def capture_target(self, token: str | None) -> dict[str, str]:
        with self.database.connect() as connection:
            auth = self._capture_auth(connection, token)
            bucket = self._require_bucket(connection, auth["bucket_id"])
            return {"bucket_id": bucket["id"], "name": bucket["name"]}

    def capture_with_token(
        self,
        token: str | None,
        items: list[dict[str, Any]],
        *,
        origin: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload, normalized = self._validated_capture(items, origin, idempotency_key)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            auth = self._capture_auth(connection, token)
            connection.execute(
                "UPDATE bucket_capture_tokens SET last_used_at=? WHERE id=?",
                (utc_now(), auth["id"]),
            )
            response = self._capture(
                connection, auth["bucket_id"], normalized, payload, auth["id"]
            )
        # A capture credential cannot list captures, metadata, jobs or secrets.
        return {key: response[key] for key in ("bucket_id", "accepted", "duplicates")}


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=401, detail="Invalid capture token")
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


def register_bucket_routes(
    app: FastAPI, control: APIRouter, database: Database
) -> None:
    store = BucketStore(database)
    app.state.bucket_store = store

    def invoke(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (ValidationError, NotFoundError, ConflictError, PermissionError) as exc:
            raise _http_error(exc) from exc

    @control.get("/buckets")
    def list_buckets() -> dict[str, Any]:
        return store.list_buckets()

    @control.post("/buckets", status_code=201)
    def create_bucket(payload: BucketCreate) -> dict[str, Any]:
        return invoke(store.create_bucket, payload.name, payload.description)

    @control.get("/buckets/summary")
    def summary() -> dict[str, Any]:
        return store.summary()

    @control.get("/buckets/{bucket_id}")
    def get_bucket(bucket_id: str) -> dict[str, Any]:
        return invoke(store.get_bucket, bucket_id)

    @control.patch("/buckets/{bucket_id}")
    def patch_bucket(bucket_id: str, payload: BucketPatch) -> dict[str, Any]:
        return invoke(
            store.update_bucket, bucket_id, **payload.model_dump(exclude_unset=True)
        )

    @control.get("/buckets/{bucket_id}/items")
    def list_items(
        bucket_id: str,
        query: Annotated[str | None, Query(alias="q", max_length=500)] = None,
        status: str | None = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        return invoke(
            store.list_items,
            bucket_id,
            query=query,
            status=status,
            limit=limit,
            offset=offset,
        )

    @control.post("/buckets/{bucket_id}/items", status_code=201)
    def capture_items(bucket_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        validated = invoke(_model, CaptureRequest, payload)
        return invoke(
            store.capture,
            bucket_id,
            [item.model_dump() for item in validated.items],
            origin=validated.origin,
            idempotency_key=validated.idempotency_key,
        )

    @control.delete("/buckets/{bucket_id}/items/{item_id}")
    def remove_item(bucket_id: str, item_id: str) -> dict[str, Any]:
        return invoke(store.remove_item, bucket_id, item_id)

    @control.post("/buckets/{bucket_id}/dispatch")
    def dispatch(bucket_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        validated = invoke(_model, DispatchRequest, payload)
        values = validated.model_dump()
        values["media_format"] = values.pop("format")
        return invoke(store.dispatch, bucket_id, **values)

    @control.post("/buckets/{bucket_id}/dispatch-runs/{run_id}/retry")
    def retry_dispatch(bucket_id: str, run_id: str) -> dict[str, Any]:
        return invoke(store.retry_dispatch, bucket_id, run_id)

    @control.get("/buckets/{bucket_id}/export")
    def export(bucket_id: str, format: Literal["text", "json"] = "text"):
        exported = invoke(store.export, bucket_id, format=format)
        if format == "text":
            return PlainTextResponse(exported)
        return exported

    @control.get("/capture-tokens")
    def list_capture_tokens() -> dict[str, Any]:
        return store.list_capture_tokens()

    @control.post("/capture-tokens", status_code=201)
    def create_capture_token(payload: CaptureTokenCreate) -> dict[str, Any]:
        return invoke(store.create_capture_token, payload.bucket_id, payload.name)

    @control.delete("/capture-tokens/{token_id}")
    def revoke_capture_token(token_id: str) -> dict[str, Any]:
        return invoke(store.revoke_capture_token, token_id)

    @app.get("/api/capture-target")
    def capture_target(
        x_ateable_capture: Annotated[
            str | None, Header(alias="X-Ateable-Capture")
        ] = None,
    ):
        return invoke(store.capture_target, x_ateable_capture)

    @app.post("/api/capture", status_code=201)
    def scoped_capture(
        payload: dict[str, Any],
        x_ateable_capture: Annotated[
            str | None, Header(alias="X-Ateable-Capture")
        ] = None,
    ):
        # Authenticate before parsing/link validation; an admin key is never
        # accepted in place of a capture credential.
        invoke(store.capture_target, x_ateable_capture)
        validated = invoke(_model, CaptureRequest, payload)
        return invoke(
            store.capture_with_token,
            x_ateable_capture,
            [item.model_dump() for item in validated.items],
            origin=validated.origin,
            idempotency_key=validated.idempotency_key,
        )
