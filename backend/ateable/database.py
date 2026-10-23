"""SQLite persistence for the Ateable controller."""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import secrets
import sqlite3
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import AppConfig
from .pairing import encode_pair_key, hash_pair_secret
from .validation import (
    ValidationError,
    is_s3_listing,
    normalize_download_url,
    parse_s3_url,
)


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


class GoneError(Exception):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Database:
    """Small SQLite repository. A fresh connection is used per operation."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.path = config.database_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.admin_key = ""

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path, timeout=30, isolation_level=None, check_same_thread=False
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.create_function(
                "casefold",
                1,
                lambda value: (value or "").casefold(),
                deterministic=True,
            )
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 30000")
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    capabilities TEXT NOT NULL,
                    max_concurrency INTEGER NOT NULL,
                    output_dir TEXT,
                    cooldown_until TEXT,
                    last_seen_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pairing_keys (
                    id TEXT PRIMARY KEY,
                    secret_hash TEXT NOT NULL,
                    controller_url TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT,
                    consumed_by TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY,
                    idempotency_key TEXT UNIQUE,
                    format TEXT NOT NULL,
                    target_worker_id TEXT,
                    duplicates TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(target_worker_id) REFERENCES workers(id)
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    canonical_url TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    format TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    stage TEXT,
                    title TEXT,
                    target_worker_id TEXT,
                    claimed_by TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    output_subdir TEXT,
                    proxy TEXT,
                    cookies_file TEXT,
                    output_path TEXT,
                    error TEXT,
                    next_attempt_at TEXT NOT NULL,
                    lease_expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(batch_id, dedupe_key),
                    FOREIGN KEY(batch_id) REFERENCES batches(id) ON DELETE CASCADE,
                    FOREIGN KEY(target_worker_id) REFERENCES workers(id),
                    FOREIGN KEY(claimed_by) REFERENCES workers(id)
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT,
                    type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_queue
                    ON jobs(status, target_worker_id, next_attempt_at, created_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_batch ON jobs(batch_id);
                CREATE INDEX IF NOT EXISTS idx_jobs_claimed ON jobs(claimed_by, status);
                CREATE INDEX IF NOT EXISTS idx_events_id ON events(id);

                CREATE TABLE IF NOT EXISTS downloaded_files (
                    job_id TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    size_bytes INTEGER,
                    downloaded_at TEXT NOT NULL,
                    present INTEGER,
                    checked_at TEXT,
                    FOREIGN KEY(worker_id) REFERENCES workers(id)
                );
                CREATE INDEX IF NOT EXISTS idx_downloaded_files_worker
                    ON downloaded_files(worker_id, downloaded_at);

                -- These metadata ledgers deliberately have no job foreign key:
                -- clearing completed queue rows must not repeat prefix downloads.
                CREATE TABLE IF NOT EXISTS discovered_objects (
                    root_job_id TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    child_job_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    size_bytes INTEGER,
                    title TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(root_job_id, dedupe_key)
                );
                CREATE TABLE IF NOT EXISTS discovery_reports (
                    job_id TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    root_job_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    response TEXT NOT NULL,
                    continuation_token_hash TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_discovery_continuations
                    ON discovery_reports(root_job_id, continuation_token_hash)
                    WHERE continuation_token_hash IS NOT NULL;
                """
            )
            defaults = {
                "default_format": "original",
                "output_dir": str(self.config.output_dir),
                "max_concurrent_downloads": str(self.config.local_concurrency),
                "worker_timeout_seconds": str(self.config.worker_timeout_seconds),
            }
            now = utc_now()
            job_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            for name, definition in {
                "kind": "TEXT NOT NULL DEFAULT 'download'",
                "task_options": "TEXT NOT NULL DEFAULT '{}'",
                "source_job_id": "TEXT",
                "root_job_id": "TEXT",
                "discovery_count": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in job_columns:
                    connection.execute(
                        f"ALTER TABLE jobs ADD COLUMN {name} {definition}"
                    )
            connection.execute(
                "UPDATE jobs SET root_job_id=id WHERE root_job_id IS NULL"
            )
            worker_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(workers)").fetchall()
            }
            if "cooldown_until" not in worker_columns:
                connection.execute("ALTER TABLE workers ADD COLUMN cooldown_until TEXT")
            if "inventory" not in worker_columns:
                connection.execute("ALTER TABLE workers ADD COLUMN inventory TEXT")
            connection.execute(
                "INSERT OR IGNORE INTO downloaded_files(job_id,worker_id,path,downloaded_at) "
                "SELECT id,claimed_by,output_path,updated_at FROM jobs "
                "WHERE status='completed' AND output_path IS NOT NULL "
                "AND claimed_by IS NOT NULL AND claimed_by != 'local'"
            )
            # Old master-owned work is retained, then assigned to a remote worker
            # when one reconnects. Never restart a legacy local download here.
            connection.execute(
                "UPDATE jobs SET status='queued',stage='queued',claimed_by=NULL,lease_expires_at=NULL,"
                "target_worker_id=NULL WHERE status='downloading' AND claimed_by='local'"
            )
            for key, value in defaults.items():
                connection.execute(
                    "INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES (?, ?, ?)",
                    (key, value, now),
                )
            if self.config.admin_key:
                self.admin_key = self.config.admin_key
            else:
                row = connection.execute(
                    "SELECT value FROM settings WHERE key = 'admin_key'"
                ).fetchone()
                if row:
                    self.admin_key = row["value"]
                else:
                    self.admin_key = secrets.token_urlsafe(36)
                    connection.execute(
                        "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)",
                        ("admin_key", self.admin_key, now),
                    )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def create_pairing_key(
        self, *, controller_url: str, expires_in_seconds: int
    ) -> dict[str, str]:
        key_id = str(uuid.uuid4())
        secret = secrets.token_urlsafe(36)
        now = datetime.now(UTC)
        created_at = now.isoformat(timespec="milliseconds")
        expires_at = (now + timedelta(seconds=expires_in_seconds)).isoformat(
            timespec="milliseconds"
        )
        pair_key = encode_pair_key(controller_url, key_id, secret)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO pairing_keys(
                    id, secret_hash, controller_url, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    key_id,
                    hash_pair_secret(controller_url, key_id, secret),
                    controller_url.rstrip("/"),
                    expires_at,
                    created_at,
                ),
            )
            self._event(
                connection,
                "pairing_key.created",
                {"pairing_key_id": key_id, "expires_at": expires_at},
            )
        return {
            "id": key_id,
            "pair_key": pair_key,
            "controller_url": controller_url.rstrip("/"),
            "expires_at": expires_at,
        }

    def revoke_unused_pairing_keys(self) -> int:
        now = utc_now()
        with self.connect() as connection:
            changed = connection.execute(
                """
                UPDATE pairing_keys
                SET consumed_at = ?, consumed_by = 'revoked'
                WHERE consumed_at IS NULL AND expires_at > ?
                """,
                (now, now),
            ).rowcount
            if changed:
                self._event(connection, "pairing_keys.revoked", {"count": changed})
        return changed

    def consume_pairing_key(
        self,
        *,
        key_id: str,
        secret: str,
        controller_url: str,
        name: str,
        worker_id: str | None,
        capabilities: list[str],
        max_concurrency: int,
        output_dir: str | None,
    ) -> tuple[dict[str, Any], str]:
        worker_id = worker_id or str(uuid.uuid4())
        if worker_id == "local":
            raise ConflictError("The master cannot be registered as a download worker")
        token = secrets.token_urlsafe(36)
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                pair = connection.execute(
                    "SELECT * FROM pairing_keys WHERE id = ?", (key_id,)
                ).fetchone()
                if not pair or not hmac.compare_digest(
                    pair["secret_hash"],
                    hash_pair_secret(controller_url, key_id, secret),
                ):
                    raise PermissionError("Invalid pairing key")
                if pair["consumed_at"]:
                    raise ConflictError("Pairing key has already been used")
                if datetime.fromisoformat(pair["expires_at"]) <= datetime.now(UTC):
                    raise GoneError("Pairing key has expired")
                try:
                    connection.execute(
                        """
                        INSERT INTO workers(
                            id, name, token_hash, capabilities, max_concurrency,
                            output_dir, cooldown_until, last_seen_at, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
                        """,
                        (
                            worker_id,
                            name,
                            _hash_token(token),
                            _json(capabilities),
                            max_concurrency,
                            output_dir,
                            now,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError(
                        f"Worker id {worker_id!r} is already registered"
                    ) from exc
                connection.execute(
                    """
                    UPDATE pairing_keys
                    SET consumed_at = ?, consumed_by = ?
                    WHERE id = ? AND consumed_at IS NULL
                    """,
                    (now, worker_id, key_id),
                )
                self._event(
                    connection,
                    "worker.paired",
                    {"worker_id": worker_id, "name": name},
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {"id": worker_id, "name": name}, token

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        event_type: str,
        payload: dict[str, Any],
        job_id: str | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO events(job_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
            (job_id, event_type, _json(payload), utc_now()),
        )

    @staticmethod
    def _job(row: sqlite3.Row) -> dict[str, Any]:
        result = {
            key: row[key]
            for key in (
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
        }
        result["task_options"] = json.loads(row["task_options"])
        return result

    @staticmethod
    def _claimed_job(row: sqlite3.Row, lease_seconds: int) -> dict[str, Any]:
        result = Database._job(row)
        result.update(
            {
                "output_subdir": row["output_subdir"],
                "proxy": row["proxy"],
                "cookies_file": row["cookies_file"],
                "lease_seconds": lease_seconds,
            }
        )
        return result

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if not row:
            raise NotFoundError(f"Job {job_id!r} was not found")
        return self._job(row)

    def list_jobs(
        self,
        *,
        status: str | None = None,
        batch_id: str | None = None,
        worker_id: str | None = None,
        query: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses: list[str] = []
        values: list[Any] = []
        if status in {"active", "attention"}:
            clauses.append("status IN (?, ?)")
            values.extend(
                ("queued", "downloading")
                if status == "active"
                else ("paused", "failed")
            )
        elif status:
            clauses.append("status = ?")
            values.append(status)
        if batch_id:
            clauses.append("batch_id = ?")
            values.append(batch_id)
        if worker_id:
            if worker_id == "local":
                clauses.append("target_worker_id IS NULL")
            else:
                clauses.append("target_worker_id = ?")
                values.append(worker_id)
        if query and query.strip():
            fields = (
                "id",
                "batch_id",
                "title",
                "source_url",
                "canonical_url",
                "format",
                "status",
                "stage",
                "target_worker_id",
                "claimed_by",
                "error",
            )
            searchable = " || ' ' || ".join(
                f"COALESCE({field}, '')" for field in fields
            )
            clauses.append(f"instr(casefold({searchable}), ?) > 0")
            values.append(query.strip().casefold())
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as connection:
            connection.execute("BEGIN")
            total = connection.execute(
                f"SELECT COUNT(*) AS count FROM jobs{where}", values
            ).fetchone()["count"]
            rows = connection.execute(
                f"SELECT * FROM jobs{where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (*values, limit, offset),
            ).fetchall()
        return [self._job(row) for row in rows], total

    def _batch(self, connection: sqlite3.Connection, batch_id: str) -> dict[str, Any]:
        batch = connection.execute(
            "SELECT * FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if not batch:
            raise NotFoundError(f"Batch {batch_id!r} was not found")
        counts = {
            row["status"]: row["count"]
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM jobs WHERE batch_id = ? GROUP BY status",
                (batch_id,),
            ).fetchall()
        }
        total = sum(counts.values())
        if total and counts.get("completed", 0) == total:
            status = "completed"
        elif total and counts.get("cancelled", 0) == total:
            status = "cancelled"
        elif counts.get("downloading", 0):
            status = "downloading"
        elif counts.get("queued", 0):
            status = "queued"
        elif counts.get("paused", 0):
            status = "paused"
        elif counts.get("failed", 0):
            status = "failed"
        else:
            status = "empty"
        return {
            "id": batch["id"],
            "status": status,
            "format": batch["format"],
            "target_worker_id": batch["target_worker_id"],
            "total": total,
            **{
                item: counts.get(item, 0)
                for item in (
                    "queued",
                    "downloading",
                    "paused",
                    "completed",
                    "failed",
                    "cancelled",
                )
            },
            "created_at": batch["created_at"],
        }

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            return self._batch(connection, batch_id)

    def enqueue_batch(
        self,
        *,
        urls: list[tuple[str, str, str]],
        media_format: str,
        target_worker_id: str | None,
        output_subdir: str | None,
        proxy: str | None,
        cookies_file: str | None,
        idempotency_key: str | None,
        max_attempts: int,
        duplicates: list[str],
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[str], bool]:
        """Insert a complete batch in one write transaction.

        urls contains (source_url, canonical_url, dedupe_key).
        """
        batch_id = str(uuid.uuid4())
        now = utc_now()
        if media_format != "original" and any(
            canonical_url.lower().startswith("s3://") for _, canonical_url, _ in urls
        ):
            raise ValidationError(
                "S3 objects preserve their original format; choose original"
            )
        if target_worker_id == "local":
            raise ConflictError(
                "Downloads run on paired workers; the master cannot download"
            )
        if target_worker_id == "auto":
            target_worker_id = None
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if idempotency_key:
                    existing = connection.execute(
                        "SELECT id, duplicates FROM batches WHERE idempotency_key = ?",
                        (idempotency_key,),
                    ).fetchone()
                    if existing:
                        jobs = connection.execute(
                            "SELECT * FROM jobs WHERE batch_id = ? ORDER BY created_at, id",
                            (existing["id"],),
                        ).fetchall()
                        batch = self._batch(connection, existing["id"])
                        connection.commit()
                        return (
                            batch,
                            [self._job(row) for row in jobs],
                            json.loads(existing["duplicates"]),
                            True,
                        )
                assigned_workers: list[str] = []
                if target_worker_id:
                    worker = connection.execute(
                        "SELECT id, capabilities FROM workers WHERE id = ? AND id != 'local'",
                        (target_worker_id,),
                    ).fetchone()
                    if not worker:
                        raise NotFoundError(
                            f"Target worker {target_worker_id!r} was not found"
                        )
                    if media_format not in json.loads(worker["capabilities"]):
                        raise ConflictError(
                            "Selected worker does not support this format; update its worker software"
                        )
                    assigned_workers = [target_worker_id]
                else:
                    threshold = (
                        datetime.now(UTC)
                        - timedelta(seconds=self.config.worker_timeout_seconds)
                    ).isoformat(timespec="milliseconds")
                    eligible = connection.execute(
                        "SELECT id, capabilities, max_concurrency FROM workers "
                        "WHERE id != 'local' AND last_seen_at >= ? ORDER BY id",
                        (threshold,),
                    ).fetchall()
                    for worker in eligible:
                        if media_format in json.loads(worker["capabilities"]):
                            assigned_workers.extend(
                                [worker["id"]]
                                * min(64, max(1, worker["max_concurrency"]))
                            )
                    if not assigned_workers:
                        raise ConflictError(
                            "Pair an online worker that supports this format before adding downloads"
                        )
                connection.execute(
                    """
                    INSERT INTO batches(
                        id, idempotency_key, format, target_worker_id, duplicates, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        idempotency_key,
                        media_format,
                        target_worker_id,
                        _json(duplicates),
                        now,
                    ),
                )
                rows: list[dict[str, Any]] = []
                for index, (source_url, canonical_url, dedupe_key) in enumerate(urls):
                    job_id = str(uuid.uuid4())
                    kind = "discover" if is_s3_listing(canonical_url) else "download"
                    connection.execute(
                        """
                        INSERT INTO jobs(
                            id, batch_id, source_url, canonical_url, dedupe_key, format,
                            status, progress, stage, target_worker_id, attempts, max_attempts,
                            output_subdir, proxy, cookies_file, next_attempt_at,
                            created_at, updated_at, kind, task_options, root_job_id
                        ) VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, 'queued', ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            job_id,
                            batch_id,
                            source_url,
                            canonical_url,
                            dedupe_key,
                            media_format,
                            assigned_workers[index % len(assigned_workers)],
                            max_attempts,
                            output_subdir,
                            proxy,
                            cookies_file,
                            now,
                            now,
                            now,
                            kind,
                            _json({"limit": 5000} if kind == "discover" else {}),
                            job_id,
                        ),
                    )
                    row = connection.execute(
                        "SELECT * FROM jobs WHERE id = ?", (job_id,)
                    ).fetchone()
                    rows.append(self._job(row))
                # Exact S3 sources already present in this batch are durable
                # members of every covering prefix. Queue cleanup must not make
                # a later listing report download those objects a second time.
                listing_roots = [
                    (item["id"], parse_s3_url(item["canonical_url"]))
                    for item in rows
                    if item["kind"] == "discover"
                ]
                for item in rows:
                    if item["kind"] != "download" or not item[
                        "canonical_url"
                    ].startswith("s3://"):
                        continue
                    location = parse_s3_url(item["canonical_url"])
                    _, identity = normalize_download_url(item["canonical_url"])
                    for root_id, source in listing_roots:
                        if location.bucket == source.bucket and location.key.startswith(
                            source.key
                        ):
                            connection.execute(
                                "INSERT OR IGNORE INTO discovered_objects(root_job_id,dedupe_key,child_job_id,"
                                "worker_id,url,size_bytes,title,created_at) VALUES(?,?,?,?,?,NULL,NULL,?)",
                                (
                                    root_id,
                                    identity,
                                    item["id"],
                                    item["target_worker_id"],
                                    item["canonical_url"],
                                    now,
                                ),
                            )
                self._event(
                    connection,
                    "batch.enqueued",
                    {
                        "batch_id": batch_id,
                        "count": len(rows),
                        "duplicate_count": len(duplicates),
                        "format": media_format,
                        "target_worker_id": target_worker_id,
                    },
                )
                batch = self._batch(connection, batch_id)
                connection.commit()
                return batch, rows, duplicates, False
            except Exception:
                connection.rollback()
                raise

    def ensure_local_worker(self) -> None:
        raise ConflictError("The master cannot be registered as a download worker")

    def authenticate_worker(self, worker_id: str, token: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM workers WHERE id = ?", (worker_id,)
            ).fetchone()
        if (
            not row
            or worker_id == "local"
            or not hmac.compare_digest(row["token_hash"], _hash_token(token))
        ):
            raise PermissionError("Invalid worker credentials")
        return dict(row)

    def heartbeat_worker(self, worker_id: str, active_job_ids: list[str]) -> None:
        now = utc_now()
        lease = (
            datetime.now(UTC) + timedelta(seconds=self.config.lease_seconds)
        ).isoformat(timespec="milliseconds")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE workers SET last_seen_at = ? WHERE id = ?", (now, worker_id)
            )
            if active_job_ids:
                placeholders = ",".join("?" for _ in active_job_ids)
                connection.execute(
                    f"""
                    UPDATE jobs SET lease_expires_at = ?, updated_at = ?
                    WHERE claimed_by = ? AND status = 'downloading'
                      AND id IN ({placeholders})
                    """,
                    (lease, now, worker_id, *active_job_ids),
                )
            connection.commit()

    def list_workers(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM workers WHERE id != 'local' ORDER BY created_at"
            ).fetchall()
            timeout_row = connection.execute(
                "SELECT value FROM settings WHERE key = 'worker_timeout_seconds'"
            ).fetchone()
        timeout_seconds = (
            int(timeout_row["value"])
            if timeout_row
            else self.config.worker_timeout_seconds
        )
        threshold = datetime.now(UTC) - timedelta(seconds=timeout_seconds)
        result = []
        for row in rows:
            seen = datetime.fromisoformat(row["last_seen_at"])
            result.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "status": "online" if seen >= threshold else "offline",
                    "last_seen_at": row["last_seen_at"],
                    "capabilities": json.loads(row["capabilities"]),
                    "max_concurrency": row["max_concurrency"],
                    "output_dir": row["output_dir"],
                    "cooldown_until": row["cooldown_until"],
                    "created_at": row["created_at"],
                    "inventory": json.loads(row["inventory"])
                    if row["inventory"]
                    else None,
                }
            )
        return result

    def _requeue_expired(self, connection: sqlite3.Connection) -> int:
        now = utc_now()
        rows = connection.execute(
            """
            SELECT id, attempts, max_attempts FROM jobs
            WHERE status = 'downloading' AND lease_expires_at IS NOT NULL
              AND lease_expires_at < ?
            """,
            (now,),
        ).fetchall()
        if not rows:
            return 0
        for row in rows:
            exhausted = row["attempts"] >= row["max_attempts"]
            connection.execute(
                """
                UPDATE jobs SET status = ?, stage = ?, claimed_by = NULL,
                    lease_expires_at = NULL, next_attempt_at = ?, updated_at = ?,
                    error = ?
                WHERE id = ? AND status = 'downloading'
                """,
                (
                    "failed" if exhausted else "queued",
                    "failed" if exhausted else "queued",
                    now,
                    now,
                    (
                        "Worker lease expired; retry limit reached"
                        if exhausted
                        else "Worker lease expired; job requeued"
                    ),
                    row["id"],
                ),
            )
            self._event(
                connection,
                "job.failed" if exhausted else "job.requeued",
                {
                    "reason": "worker lease expired",
                    "retry_limit_reached": exhausted,
                },
                row["id"],
            )
        return len(rows)

    def report_inventory(
        self, worker_id: str, inventory: dict[str, Any], files: list[dict[str, Any]]
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE workers SET inventory = ? WHERE id = ?",
                (_json(inventory), worker_id),
            )
            for file in files:
                connection.execute(
                    "UPDATE downloaded_files SET present=?,size_bytes=COALESCE(?,size_bytes),checked_at=? "
                    "WHERE worker_id=? AND job_id=?",
                    (
                        int(file["exists"]),
                        file.get("size_bytes"),
                        now,
                        worker_id,
                        file["job_id"],
                    ),
                )
            connection.commit()

    def update_worker_profile(self, worker_id: str, profile: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE workers SET name=?,capabilities=?,max_concurrency=?,output_dir=?,last_seen_at=? WHERE id=?",
                (
                    profile["name"],
                    _json(profile["capabilities"]),
                    profile["max_concurrency"],
                    profile["output_dir"],
                    utc_now(),
                    worker_id,
                ),
            )

    def _assign_legacy_jobs(self, connection: sqlite3.Connection) -> None:
        threshold = (
            datetime.now(UTC) - timedelta(seconds=self.config.worker_timeout_seconds)
        ).isoformat(timespec="milliseconds")
        workers = connection.execute(
            "SELECT id,capabilities,max_concurrency FROM workers WHERE id != 'local' AND last_seen_at>=? ORDER BY id",
            (threshold,),
        ).fetchall()
        slots: dict[str, list[str]] = {}
        for worker in workers:
            for media_format in json.loads(worker["capabilities"]):
                slots.setdefault(media_format, []).extend(
                    [worker["id"]] * min(64, max(1, worker["max_concurrency"]))
                )
        rows = connection.execute(
            "SELECT id,format FROM jobs WHERE status='queued' AND (target_worker_id IS NULL OR target_worker_id='local') ORDER BY created_at,id"
        ).fetchall()
        counts: dict[str, int] = {}
        assigned = 0
        for row in rows:
            available = slots.get(row["format"], [])
            if available:
                index = counts.get(row["format"], 0)
                counts[row["format"]] = index + 1
                connection.execute(
                    "UPDATE jobs SET target_worker_id=? WHERE id=?",
                    (available[index % len(available)], row["id"]),
                )
                assigned += 1
        if assigned:
            self._event(connection, "jobs.migrated_to_workers", {"count": assigned})

    def worker_storage(self, worker_id: str, limit: int = 100) -> dict[str, Any]:
        with self.connect() as connection:
            worker = connection.execute(
                "SELECT inventory FROM workers WHERE id=? AND id != 'local'",
                (worker_id,),
            ).fetchone()
            if worker is None:
                raise NotFoundError("Worker was not found")
            rows = connection.execute(
                "SELECT job_id,path,size_bytes,downloaded_at,present,checked_at FROM downloaded_files "
                "WHERE worker_id=? ORDER BY downloaded_at DESC,job_id DESC LIMIT ?",
                (worker_id, limit),
            ).fetchall()
            totals = connection.execute(
                "SELECT COUNT(*) AS count,COALESCE(SUM(size_bytes),0) AS bytes FROM downloaded_files WHERE worker_id=?",
                (worker_id,),
            ).fetchone()
        return {
            "inventory": json.loads(worker["inventory"])
            if worker["inventory"]
            else None,
            "files": [
                {
                    "job_id": row["job_id"],
                    "path": row["path"],
                    "size_bytes": row["size_bytes"],
                    "downloaded_at": row["downloaded_at"],
                    "exists": bool(row["present"])
                    if row["present"] is not None
                    else None,
                    "checked_at": row["checked_at"],
                }
                for row in rows
            ],
            "total_downloaded_bytes": totals["bytes"],
            "completed_files": totals["count"],
        }

    def claim_jobs(
        self,
        *,
        worker_id: str,
        capabilities: Iterable[str],
        limit: int,
        local: bool = False,
    ) -> list[dict[str, Any]]:
        if local or worker_id == "local":
            raise ConflictError("The master cannot claim download jobs")
        now = utc_now()
        expires = (
            datetime.now(UTC) + timedelta(seconds=self.config.lease_seconds)
        ).isoformat(timespec="milliseconds")
        formats = list(capabilities)
        if not formats:
            return []
        placeholders = ",".join("?" for _ in formats)
        target_clause = "target_worker_id IS NULL" if local else "target_worker_id = ?"
        params: list[Any] = [*formats]
        if not local:
            params.append(worker_id)
        params.append(now)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._requeue_expired(connection)
                self._assign_legacy_jobs(connection)
                worker = connection.execute(
                    "SELECT cooldown_until, max_concurrency FROM workers WHERE id = ?",
                    (worker_id,),
                ).fetchone()
                if not worker:
                    raise NotFoundError(f"Worker {worker_id!r} was not found")
                connection.execute(
                    "UPDATE workers SET last_seen_at = ? WHERE id = ?",
                    (now, worker_id),
                )
                if worker["cooldown_until"] and worker["cooldown_until"] > now:
                    connection.commit()
                    return []
                if worker["cooldown_until"]:
                    connection.execute(
                        "UPDATE workers SET cooldown_until = NULL WHERE id = ?",
                        (worker_id,),
                    )
                # Count and claim under the same write transaction so overlapping
                # requests cannot each spend the worker's remaining capacity.
                active = connection.execute(
                    "SELECT COUNT(*) FROM jobs "
                    "WHERE claimed_by = ? AND status = 'downloading'",
                    (worker_id,),
                ).fetchone()[0]
                available = min(limit, max(0, worker["max_concurrency"] - active))
                if available <= 0:
                    connection.commit()
                    return []
                rows = connection.execute(
                    f"""
                    SELECT * FROM jobs
                    WHERE status = 'queued'
                      AND format IN ({placeholders})
                      AND {target_clause}
                      AND next_attempt_at <= ?
                    ORDER BY created_at, id
                    LIMIT ?
                    """,
                    (*params, available),
                ).fetchall()
                claimed = []
                for row in rows:
                    changed = connection.execute(
                        """
                        UPDATE jobs SET status = 'downloading',
                            stage = CASE WHEN kind='discover' THEN 'discovering' ELSE 'starting' END,
                            claimed_by = ?, attempts = attempts + 1,
                            lease_expires_at = ?, updated_at = ?, error = NULL
                        WHERE id = ? AND status = 'queued'
                        """,
                        (worker_id, expires, now, row["id"]),
                    ).rowcount
                    if not changed:
                        continue
                    current = connection.execute(
                        "SELECT * FROM jobs WHERE id = ?", (row["id"],)
                    ).fetchone()
                    claimed.append(
                        self._claimed_job(current, self.config.lease_seconds)
                    )
                    self._event(
                        connection,
                        "job.started",
                        {"worker_id": worker_id, "attempt": current["attempts"]},
                        row["id"],
                    )
                connection.commit()
                return claimed
            except Exception:
                connection.rollback()
                raise

    def report_progress(
        self,
        *,
        worker_id: str,
        job_id: str,
        progress: float | None,
        stage: str,
        title: str | None,
        metrics: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now()
        expires = (
            datetime.now(UTC) + timedelta(seconds=self.config.lease_seconds)
        ).isoformat(timespec="milliseconds")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not row:
                connection.rollback()
                raise NotFoundError(f"Job {job_id!r} was not found")
            if row["status"] != "downloading" or row["claimed_by"] != worker_id:
                connection.rollback()
                raise ConflictError(
                    f"Job is {row['status']!r} and is no longer owned by this worker"
                )
            next_progress = (
                row["progress"]
                if progress is None
                else max(row["progress"], min(99.5, progress))
            )
            connection.execute(
                """
                UPDATE jobs SET progress = ?, stage = ?, title = COALESCE(?, title),
                    lease_expires_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (next_progress, stage, title, expires, now, job_id),
            )
            if abs(next_progress - row["progress"]) >= 0.5 or stage != row["stage"]:
                self._event(
                    connection,
                    "job.progress",
                    {"progress": next_progress, "stage": stage, **metrics},
                    job_id,
                )
            current = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            connection.commit()
        return self._job(current)

    def complete_discovery(
        self,
        *,
        worker_id: str,
        job_id: str,
        items: list[dict[str, Any]],
        continuation_token: str | None = None,
    ) -> dict[str, Any]:
        """Atomically record one worker listing and queue its exact object jobs.

        Validation and URL parsing are entirely local. Durable report and object
        ledgers preserve replay safety even after completed queue rows are cleared.
        """
        if not isinstance(items, list) or len(items) > 5000:
            raise ValidationError("Discovery report must contain at most 5000 objects")
        if continuation_token is not None and (
            not isinstance(continuation_token, str)
            or not continuation_token
            or len(continuation_token) > 16384
        ):
            raise ValidationError("Invalid discovery continuation token")
        request_hash = hashlib.sha256(
            json.dumps(
                {"items": items, "continuation_token": continuation_token},
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                replay = connection.execute(
                    "SELECT * FROM discovery_reports WHERE job_id=?", (job_id,)
                ).fetchone()
                if replay:
                    if replay["worker_id"] != worker_id:
                        raise ConflictError(
                            "Discovery report belongs to another worker"
                        )
                    if replay["request_hash"] != request_hash:
                        raise ConflictError(
                            "Discovery report was already completed with different metadata"
                        )
                    connection.commit()
                    return json.loads(replay["response"])
                row = connection.execute(
                    "SELECT * FROM jobs WHERE id=?", (job_id,)
                ).fetchone()
                if not row:
                    raise NotFoundError(f"Job {job_id!r} was not found")
                if row["status"] != "downloading" or row["claimed_by"] != worker_id:
                    raise ConflictError("Job is no longer owned by this worker")
                if row["kind"] != "discover":
                    raise ConflictError(
                        "Only discovery jobs may report discovered objects"
                    )
                source = parse_s3_url(row["canonical_url"])
                if not source.is_listing or row["format"] != "original":
                    raise ConflictError("Job is not an original S3 prefix discovery")
                options = json.loads(row["task_options"])
                if len(items) > options.get("limit", 5000):
                    raise ValidationError(
                        "Discovery report exceeds the assigned page limit"
                    )
                root_job_id = row["root_job_id"] or job_id
                token_hash = None
                if continuation_token is not None:
                    if continuation_token == options.get("continuation_token"):
                        raise ValidationError(
                            "Discovery continuation token did not advance"
                        )
                    token_hash = hashlib.sha256(
                        continuation_token.encode("utf-8")
                    ).hexdigest()
                    if connection.execute(
                        "SELECT 1 FROM discovery_reports WHERE root_job_id=? AND continuation_token_hash=?",
                        (root_job_id, token_hash),
                    ).fetchone():
                        raise ValidationError(
                            "Discovery continuation token repeats an earlier page"
                        )
                validated: list[tuple[str, str, int, str | None]] = []
                seen: set[str] = set()
                for item in items:
                    if not isinstance(item, dict):
                        raise ValidationError("Discovery object metadata is invalid")
                    url = item.get("url")
                    key = item.get("key")
                    size = item.get("size_bytes")
                    title = item.get("title")
                    if (
                        not isinstance(url, str)
                        or not isinstance(key, str)
                        or not isinstance(size, int)
                        or isinstance(size, bool)
                        or size < 0
                        or size > 2**63 - 1
                        or (title is not None and not isinstance(title, str))
                    ):
                        raise ValidationError("Discovery object metadata is invalid")
                    canonical, dedupe_key = normalize_download_url(url)
                    location = parse_s3_url(canonical)
                    if (
                        location.is_listing
                        or location.bucket != source.bucket
                        or not location.key.startswith(source.key)
                        or location.key != key
                    ):
                        raise ValidationError(
                            "Discovered objects must be exact keys inside the assigned bucket and prefix"
                        )
                    if dedupe_key not in seen:
                        seen.add(dedupe_key)
                        validated.append(
                            (
                                canonical,
                                dedupe_key,
                                size,
                                title[:1000] if title else None,
                            )
                        )
                added_count = 0
                discovered_count = 0
                for canonical, dedupe_key, size, title in validated:
                    known = connection.execute(
                        "SELECT size_bytes FROM discovered_objects WHERE root_job_id=? AND dedupe_key=?",
                        (root_job_id, dedupe_key),
                    ).fetchone()
                    if known:
                        if known["size_bytes"] is None:
                            connection.execute(
                                "UPDATE discovered_objects SET size_bytes=?,title=? WHERE root_job_id=? AND dedupe_key=?",
                                (size, title, root_job_id, dedupe_key),
                            )
                            discovered_count += 1
                        continue
                    existing = connection.execute(
                        "SELECT id FROM jobs WHERE batch_id=? AND dedupe_key=?",
                        (row["batch_id"], dedupe_key),
                    ).fetchone()
                    child_job_id = existing["id"] if existing else str(uuid.uuid4())
                    if not existing:
                        connection.execute(
                            """
                            INSERT INTO jobs(
                                id,batch_id,source_url,canonical_url,dedupe_key,format,
                                status,progress,stage,target_worker_id,max_attempts,
                                output_subdir,proxy,cookies_file,next_attempt_at,
                                created_at,updated_at,kind,task_options,source_job_id,root_job_id,title
                            ) VALUES(?,?,?,?,?,'original','queued',0,'queued',?,?,?,?,?,?,?,?,
                                'download','{}',?,?,?)
                            """,
                            (
                                child_job_id,
                                row["batch_id"],
                                canonical,
                                canonical,
                                dedupe_key,
                                worker_id,
                                row["max_attempts"],
                                row["output_subdir"],
                                row["proxy"],
                                row["cookies_file"],
                                now,
                                now,
                                now,
                                job_id,
                                root_job_id,
                                title,
                            ),
                        )
                        added_count += 1
                    connection.execute(
                        "INSERT INTO discovered_objects(root_job_id,dedupe_key,child_job_id,worker_id,url,size_bytes,title,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (
                            root_job_id,
                            dedupe_key,
                            child_job_id,
                            worker_id,
                            canonical,
                            size,
                            title,
                            now,
                        ),
                    )
                    discovered_count += 1
                continuation_job_id = None
                if continuation_token is not None:
                    continuation_job_id = str(uuid.uuid4())
                    connection.execute(
                        """
                        INSERT INTO jobs(
                            id,batch_id,source_url,canonical_url,dedupe_key,format,
                            status,progress,stage,target_worker_id,max_attempts,
                            output_subdir,proxy,cookies_file,next_attempt_at,
                            created_at,updated_at,kind,task_options,source_job_id,root_job_id
                        ) VALUES(?,?,?,?,?,'original','queued',0,'queued',?,?,?,?,?,?,?,?,
                            'discover',?,?,?)
                        """,
                        (
                            continuation_job_id,
                            row["batch_id"],
                            row["source_url"],
                            row["canonical_url"],
                            f"discover:{root_job_id}:{token_hash}",
                            worker_id,
                            row["max_attempts"],
                            row["output_subdir"],
                            row["proxy"],
                            row["cookies_file"],
                            now,
                            now,
                            now,
                            _json(
                                {
                                    "limit": options.get("limit", 5000),
                                    "continuation_token": continuation_token,
                                }
                            ),
                            job_id,
                            root_job_id,
                        ),
                    )
                connection.execute(
                    "UPDATE jobs SET status='completed',progress=100,stage='expanded',"
                    "discovery_count=?,output_path=NULL,error=NULL,lease_expires_at=NULL,updated_at=? WHERE id=?",
                    (discovered_count, now, job_id),
                )
                if root_job_id != job_id:
                    connection.execute(
                        "UPDATE jobs SET discovery_count=discovery_count+?,updated_at=? WHERE id=?",
                        (discovered_count, now, root_job_id),
                    )
                current = connection.execute(
                    "SELECT * FROM jobs WHERE id=?", (job_id,)
                ).fetchone()
                result = {
                    "job": self._job(current),
                    "added_count": added_count,
                    "continuation_job_id": continuation_job_id,
                }
                connection.execute(
                    "INSERT INTO discovery_reports(job_id,worker_id,root_job_id,request_hash,response,continuation_token_hash,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        job_id,
                        worker_id,
                        root_job_id,
                        request_hash,
                        _json(result),
                        token_hash,
                        now,
                    ),
                )
                self._event(
                    connection,
                    "job.expanded",
                    {
                        "worker_id": worker_id,
                        "discovered_count": discovered_count,
                        "added_count": added_count,
                        "continuation_job_id": continuation_job_id,
                    },
                    job_id,
                )
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise

    def complete_job(
        self,
        *,
        worker_id: str,
        job_id: str,
        output_path: str,
        title: str | None,
        size_bytes: int | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not row:
                connection.rollback()
                raise NotFoundError(f"Job {job_id!r} was not found")
            if row["status"] != "downloading" or row["claimed_by"] != worker_id:
                connection.rollback()
                raise ConflictError("Job is no longer owned by this worker")
            if row["kind"] == "discover":
                connection.rollback()
                raise ConflictError(
                    "Discovery jobs must report discovered objects instead of a downloaded file"
                )
            connection.execute(
                """
                UPDATE jobs SET status = 'completed', progress = 100, stage = 'completed',
                    output_path = ?, title = COALESCE(?, title), error = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (output_path, title, now, job_id),
            )
            self._event(
                connection,
                "job.completed",
                {"output_path": output_path, "size_bytes": size_bytes},
                job_id,
            )
            connection.execute(
                "INSERT INTO downloaded_files(job_id,worker_id,path,size_bytes,downloaded_at,present,checked_at) "
                "VALUES(?,?,?,?,?,1,?) ON CONFLICT(job_id) DO UPDATE SET "
                "worker_id=excluded.worker_id,path=excluded.path,size_bytes=excluded.size_bytes,"
                "downloaded_at=excluded.downloaded_at,present=1,checked_at=excluded.checked_at",
                (job_id, worker_id, output_path, size_bytes, now, now),
            )
            current = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            connection.commit()
        return self._job(current)

    def fail_job(
        self,
        *,
        worker_id: str,
        job_id: str,
        error: str,
        retryable: bool,
        failure_kind: str = "generic",
        cooldown_seconds: float | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not row:
                connection.rollback()
                raise NotFoundError(f"Job {job_id!r} was not found")
            if row["status"] != "downloading" or row["claimed_by"] != worker_id:
                connection.rollback()
                raise ConflictError("Job is no longer owned by this worker")
            will_retry = retryable and row["attempts"] < row["max_attempts"]
            if will_retry:
                if failure_kind == "rate_limited":
                    base = max(120.0, cooldown_seconds or 0)
                    delay = min(3600.0, base + random.uniform(base * 0.25, base * 0.75))
                else:
                    delay = min(
                        300.0,
                        (2 ** max(0, row["attempts"] - 1)) + random.random(),
                    )
                next_attempt = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat(
                    timespec="milliseconds"
                )
                status, stage = "queued", "retrying"
            else:
                next_attempt = now
                status, stage = "failed", "failed"
                delay = 0.0
            cooldown_until = None
            if failure_kind == "rate_limited":
                base = max(120.0, cooldown_seconds or 0)
                worker_delay = max(
                    delay, base + random.uniform(base * 0.25, base * 0.75)
                )
                cooldown_until = (
                    datetime.now(UTC) + timedelta(seconds=worker_delay)
                ).isoformat(timespec="milliseconds")
                connection.execute(
                    "UPDATE workers SET cooldown_until = ? WHERE id = ?",
                    (cooldown_until, worker_id),
                )
            connection.execute(
                """
                UPDATE jobs SET status = ?, stage = ?, error = ?, claimed_by = NULL,
                    lease_expires_at = NULL, next_attempt_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, stage, error, next_attempt, now, job_id),
            )
            self._event(
                connection,
                "job.retry_scheduled" if will_retry else "job.failed",
                {
                    "error": error,
                    "retryable": retryable,
                    "failure_kind": failure_kind,
                    "cooldown_seconds": delay,
                    "worker_cooldown_until": cooldown_until,
                },
                job_id,
            )
            current = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            connection.commit()
        return self._job(current)

    def apply_action(
        self,
        *,
        action: str,
        job_ids: list[str],
        status: str | None,
        batch_id: str | None,
    ) -> tuple[int, list[dict[str, Any]], dict[str, str]]:
        if not job_ids and not status and not batch_id:
            raise ConflictError("Provide job_ids, status, or batch_id as a selector")
        clauses: list[str] = []
        values: list[Any] = []
        if job_ids:
            clauses.append(f"id IN ({','.join('?' for _ in job_ids)})")
            values.extend(job_ids)
        if status:
            clauses.append("status = ?")
            values.append(status)
        if batch_id:
            clauses.append("batch_id = ?")
            values.append(batch_id)
        valid_from = {
            "pause": {"queued", "downloading"},
            "resume": {"paused"},
            "cancel": {"queued", "downloading", "paused", "failed"},
            "retry": {"failed", "cancelled"},
        }[action]
        destination = {
            "pause": "paused",
            "resume": "queued",
            "cancel": "cancelled",
            "retry": "queued",
        }[action]
        now = utc_now()
        errors: dict[str, str] = {}
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"SELECT * FROM jobs WHERE {' AND '.join(clauses)}", values
            ).fetchall()
            changed_ids = []
            for row in rows:
                if row["status"] not in valid_from:
                    if job_ids:
                        errors[row["id"]] = (
                            f"Cannot {action} a job in {row['status']} state"
                        )
                    continue
                progress = 0 if action == "retry" else row["progress"]
                stage = "queued" if destination == "queued" else destination
                connection.execute(
                    """
                    UPDATE jobs SET status = ?, stage = ?, progress = ?,
                        claimed_by = NULL, lease_expires_at = NULL,
                        next_attempt_at = ?, error = CASE WHEN ? = 'retry' THEN NULL ELSE error END,
                        attempts = CASE WHEN ? = 'retry' THEN 0 ELSE attempts END,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        destination,
                        stage,
                        progress,
                        now,
                        action,
                        action,
                        now,
                        row["id"],
                    ),
                )
                changed_ids.append(row["id"])
                self._event(
                    connection,
                    f"job.{destination}",
                    {"action": action},
                    row["id"],
                )
            items: list[dict[str, Any]] = []
            if changed_ids:
                preview_ids = changed_ids[:100]
                preview_placeholders = ",".join("?" for _ in preview_ids)
                current = connection.execute(
                    f"SELECT * FROM jobs WHERE id IN ({preview_placeholders})",
                    preview_ids,
                ).fetchall()
                items = [self._job(row) for row in current]
            if job_ids:
                found = {row["id"] for row in rows}
                for missing in set(job_ids) - found:
                    errors[missing] = "Job was not found"
            connection.commit()
        return len(changed_ids), items, errors

    def clear_completed(self) -> int:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE status = 'completed'"
            ).fetchone()["count"]
            connection.execute(
                "DELETE FROM events WHERE job_id IN "
                "(SELECT id FROM jobs WHERE status = 'completed')"
            )
            connection.execute("DELETE FROM jobs WHERE status = 'completed'")
            connection.execute(
                "DELETE FROM batches WHERE NOT EXISTS "
                "(SELECT 1 FROM jobs WHERE jobs.batch_id = batches.id)"
            )
            self._event(connection, "jobs.cleared", {"deleted": count})
            connection.commit()
        return count

    def stats(self) -> dict[str, int]:
        with self.connect() as connection:
            counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
                ).fetchall()
            }
        workers = self.list_workers()
        return {
            "total": sum(counts.values()),
            **{
                status: counts.get(status, 0)
                for status in (
                    "queued",
                    "downloading",
                    "paused",
                    "completed",
                    "failed",
                    "cancelled",
                )
            },
            "active_workers": sum(worker["status"] == "online" for worker in workers),
        }

    def events(self, after_id: int, limit: int) -> tuple[list[dict[str, Any]], int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?",
                (after_id, limit),
            ).fetchall()
        items = [
            {
                "id": row["id"],
                "job_id": row["job_id"],
                "type": row["type"],
                "payload": json.loads(row["payload"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
        return items, (items[-1]["id"] if items else after_id)

    def get_settings(self) -> dict[str, Any]:
        with self.connect() as connection:
            values = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key, value FROM settings WHERE key != 'admin_key'"
                ).fetchall()
            }
        return {
            "default_format": values["default_format"],
            "output_dir": values["output_dir"],
            "max_concurrent_downloads": int(values["max_concurrent_downloads"]),
            "worker_timeout_seconds": int(values["worker_timeout_seconds"]),
        }

    def patch_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for key, value in values.items():
                if value is None:
                    continue
                if key == "output_dir":
                    output = Path(value).expanduser().resolve()
                    value = str(output)
                connection.execute(
                    """
                    INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (key, str(value), now),
                )
            connection.commit()
        return self.get_settings()
