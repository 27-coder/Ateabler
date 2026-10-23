"""Embedded controller-side worker."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from .database import ConflictError, Database, NotFoundError
from .downloader import (
    Downloader,
    DownloadFailure,
    DownloadSpec,
    DownloadStopped,
)
from .network import effective_proxy, probe_network

logger = logging.getLogger(__name__)


class EmbeddedWorker:
    def __init__(
        self,
        database: Database,
        downloader: Downloader,
        concurrency: int,
    ):
        self.database = database
        self.downloader = downloader
        self.concurrency = concurrency
        self._tasks: list[asyncio.Task[None]] = []
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._active: set[str] = set()
        self._network_ready = database.config.network_mode != "tor"
        self._network_check_due = 0.0
        self._network_lock = asyncio.Lock()

    async def start(self) -> None:
        self.database.ensure_local_worker()
        self._tasks = [
            asyncio.create_task(self._run_slot(index), name=f"ateable-local-{index}")
            for index in range(self.concurrency)
        ]
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name="ateable-local-heartbeat"
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        for task in self._tasks:
            task.cancel()
        pending = [*self._tasks]
        if self._heartbeat_task:
            pending.append(self._heartbeat_task)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        self._heartbeat_task = None

    async def _heartbeat_loop(self) -> None:
        interval = max(1.0, min(10.0, self.database.config.lease_seconds / 3))
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(
                    self.database.heartbeat_worker, "local", list(self._active)
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Embedded worker heartbeat failed")
            await asyncio.sleep(interval)

    async def _run_slot(self, index: int) -> None:
        capabilities = ["wav", "mp3", "m4a", "flac", "opus", "mp4"]
        while not self._stop.is_set():
            try:
                if not await self._network_available():
                    await asyncio.sleep(1)
                    continue
                claimed = self.database.claim_jobs(
                    worker_id="local",
                    capabilities=capabilities,
                    limit=1,
                    local=True,
                )
                if not claimed:
                    await asyncio.sleep(0.75)
                    continue
                job = claimed[0]
                self._active.add(job["id"])
                try:
                    await asyncio.to_thread(self._download_one, job)
                finally:
                    self._active.discard(job["id"])
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Embedded worker slot %s failed", index)
                await asyncio.sleep(1)

    async def _network_available(self) -> bool:
        if self.database.config.network_mode != "tor":
            return True
        loop = asyncio.get_running_loop()
        now = loop.time()
        if now < self._network_check_due:
            return self._network_ready
        async with self._network_lock:
            now = loop.time()
            if now < self._network_check_due:
                return self._network_ready
            status = await asyncio.to_thread(
                probe_network,
                "tor",
                None,
                self.database.config.tor_proxy,
                timeout=10,
            )
            self._network_ready = status.reachable
            self._network_check_due = now + (60 if status.reachable else 10)
            if not status.reachable:
                logger.error("%s; refusing direct fallback", status.message)
            return self._network_ready

    def _download_one(self, job: dict[str, Any]) -> None:
        network_mode = self.database.config.network_mode
        if network_mode == "tor":
            selected_proxy = effective_proxy(
                "tor", None, self.database.config.tor_proxy
            )
        elif network_mode == "proxy":
            selected_proxy = effective_proxy(
                "proxy", self.database.config.proxy
            )
        else:
            selected_proxy = job.get("proxy")
        spec = DownloadSpec(
            job_id=job["id"],
            url=job["canonical_url"],
            media_format=job["format"],
            output_subdir=job.get("output_subdir"),
            proxy=selected_proxy,
            cookies_file=job.get("cookies_file"),
            network_mode=network_mode,
        )

        def progress(update: dict[str, Any]) -> None:
            if self._stop.is_set():
                raise DownloadStopped("Worker is shutting down")
            metrics = {
                key: update.get(key)
                for key in (
                    "downloaded_bytes",
                    "total_bytes",
                    "speed",
                    "eta",
                )
                if update.get(key) is not None
            }
            try:
                self.database.report_progress(
                    worker_id="local",
                    job_id=job["id"],
                    progress=update.get("progress"),
                    stage=update.get("stage") or "downloading",
                    title=update.get("title"),
                    metrics=metrics,
                )
            except ConflictError as exc:
                raise DownloadStopped(str(exc)) from exc

        try:
            configured_output = Path(self.database.get_settings()["output_dir"])
            result = self.downloader.download(spec, configured_output, progress)
            self.database.complete_job(
                worker_id="local",
                job_id=job["id"],
                output_path=result.output_path,
                title=result.title,
            )
        except DownloadStopped:
            logger.info("Local job %s stopped by coordinator state", job["id"])
        except DownloadFailure as exc:
            try:
                self.database.fail_job(
                    worker_id="local",
                    job_id=job["id"],
                    error=str(exc),
                    retryable=exc.retryable,
                    failure_kind=exc.failure_kind,
                    cooldown_seconds=exc.cooldown_seconds,
                )
            except (ConflictError, NotFoundError):
                pass
        except Exception as exc:
            logger.exception("Unexpected downloader error for job %s", job["id"])
            try:
                self.database.fail_job(
                    worker_id="local",
                    job_id=job["id"],
                    error=f"{exc.__class__.__name__}: {exc}",
                    retryable=True,
                )
            except (ConflictError, NotFoundError):
                pass
