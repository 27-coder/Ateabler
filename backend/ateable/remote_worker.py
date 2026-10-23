"""Standalone Ateable worker process."""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .downloader import (
    Downloader,
    DownloadFailure,
    DownloadSpec,
    DownloadStopped,
    YtDlpDownloader,
)
from .network import (
    DEFAULT_TOR_PROXY,
    control_request_opener,
    effective_proxy,
    probe_network,
)
from .pairing import decode_pair_key
from .worker_inventory import WorkerInventoryTracker

logger = logging.getLogger(__name__)


class CoordinatorError(RuntimeError):
    def __init__(self, status: int | None, message: str):
        super().__init__(message)
        self.status = status


class CoordinatorClient:
    def __init__(self, base_url: str, *, opener: Callable[..., Any] | None = None):
        self.base_url = base_url.rstrip("/")
        self._opener = opener if opener is not None else control_request_opener()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
    ) -> Any:
        encoded = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=encoded,
            method=method,
            headers={
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if encoded is not None else {}),
                **(headers or {}),
            },
        )
        try:
            with self._opener(request, timeout=timeout) as response:
                data = response.read()
                return json.loads(data) if data else None
        except urllib.error.HTTPError as exc:
            try:
                if 300 <= exc.code < 400:
                    raise CoordinatorError(
                        exc.code,
                        "Controller redirects are blocked; configure its final URL",
                    ) from None
                data = exc.read()
                try:
                    detail = json.loads(data).get("detail")
                except (ValueError, AttributeError):
                    detail = data.decode("utf-8", errors="replace") or exc.reason
            finally:
                exc.close()
            raise CoordinatorError(exc.code, str(detail)) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise CoordinatorError(None, f"Coordinator unavailable: {exc}") from exc

    def pair(
        self,
        *,
        pair_key: str,
        name: str,
        worker_id: str | None,
        concurrency: int,
        output_dir: Path,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/api/workers/pair",
            body={
                "pair_key": pair_key,
                "name": name,
                "worker_id": worker_id,
                "max_concurrency": concurrency,
                "output_dir": str(output_dir.resolve()),
                "capabilities": [
                    "original",
                    "wav",
                    "mp3",
                    "m4a",
                    "flac",
                    "opus",
                    "mp4",
                ],
            },
        )

    @staticmethod
    def _auth(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def heartbeat(self, worker_id: str, token: str, active: list[str]) -> None:
        self.request(
            "POST",
            f"/api/workers/{worker_id}/heartbeat",
            body={"active_job_ids": active},
            headers=self._auth(token),
        )

    def claim(self, worker_id: str, token: str, limit: int) -> list[dict[str, Any]]:
        response = self.request(
            "POST",
            f"/api/workers/{worker_id}/claim",
            body={"limit": limit},
            headers=self._auth(token),
        )
        return response["items"]

    def inventory(self, worker_id: str, token: str, report: dict[str, Any]) -> None:
        self.request(
            "POST",
            f"/api/workers/{worker_id}/inventory",
            body=report,
            headers=self._auth(token),
        )

    def profile(self, worker_id: str, token: str, profile: dict[str, Any]) -> None:
        self.request(
            "POST",
            f"/api/workers/{worker_id}/profile",
            body=profile,
            headers=self._auth(token),
        )

    def progress(
        self,
        worker_id: str,
        token: str,
        job_id: str,
        update: dict[str, Any],
    ) -> None:
        self.request(
            "POST",
            f"/api/workers/{worker_id}/jobs/{job_id}/progress",
            body=update,
            headers=self._auth(token),
        )

    def complete(
        self,
        worker_id: str,
        token: str,
        job_id: str,
        output_path: str,
        title: str | None,
        size_bytes: int | None = None,
    ) -> None:
        self.request(
            "POST",
            f"/api/workers/{worker_id}/jobs/{job_id}/complete",
            body={"output_path": output_path, "title": title, "size_bytes": size_bytes},
            headers=self._auth(token),
        )

    def fail(
        self,
        worker_id: str,
        token: str,
        job_id: str,
        error: str,
        retryable: bool,
        failure_kind: str = "generic",
        cooldown_seconds: float | None = None,
    ) -> None:
        self.request(
            "POST",
            f"/api/workers/{worker_id}/jobs/{job_id}/fail",
            body={
                "error": error[:8000],
                "retryable": retryable,
                "failure_kind": failure_kind,
                "cooldown_seconds": cooldown_seconds,
            },
            headers=self._auth(token),
        )

    def discover(
        self,
        worker_id: str,
        token: str,
        job_id: str,
        items: list[dict[str, Any]],
        continuation_token: str | None = None,
    ) -> dict[str, Any]:
        # The controller commits reports idempotently, so a lost response can be
        # retried safely without listing the remote bucket a second time.
        for attempt in range(3):
            try:
                return self.request(
                    "POST",
                    f"/api/workers/{worker_id}/jobs/{job_id}/discover",
                    body={"items": items, "continuation_token": continuation_token},
                    headers=self._auth(token),
                )
            except CoordinatorError as exc:
                if attempt == 2 or (exc.status is not None and exc.status < 500):
                    raise
                time.sleep(attempt + 1)
        raise AssertionError("Unreachable discovery report retry state")


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    coordinator_url: str
    name: str
    pair_key: str | None
    worker_id: str | None
    worker_token: str | None
    output_dir: Path
    concurrency: int
    credentials_file: Path
    proxy: str | None
    network_mode: str
    tor_proxy: str
    cookies_file: str | None
    poll_interval: float = 1.0


class RemoteWorker:
    def __init__(self, config: WorkerConfig, downloader: Downloader | None = None):
        self.config = config
        self.client = CoordinatorClient(config.coordinator_url)
        self.downloader = downloader or YtDlpDownloader()
        self.worker_id = ""
        self.worker_token = ""
        self._pool = ThreadPoolExecutor(
            max_workers=config.concurrency, thread_name_prefix="ateable"
        )
        self._futures: dict[str, Future[None]] = {}
        self.inventory = WorkerInventoryTracker(
            config.output_dir,
            config.credentials_file.with_name("download-inventory.json"),
        )
        self._inventory_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="ateable-inventory"
        )
        self._inventory_future: Future[dict[str, Any]] | None = None

    def _load_credentials(self) -> tuple[str, str] | None:
        if self.config.worker_id and self.config.worker_token:
            return self.config.worker_id, self.config.worker_token
        try:
            value = json.loads(self.config.credentials_file.read_text(encoding="utf-8"))
            if (
                value.get("coordinator_url") == self.config.coordinator_url.rstrip("/")
                and value.get("worker_id")
                and value.get("token")
            ):
                return value["worker_id"], value["token"]
        except (OSError, ValueError, TypeError):
            return None
        return None

    def _save_credentials(self, worker_id: str, token: str) -> None:
        self.config.credentials_file.parent.mkdir(parents=True, exist_ok=True)
        self.config.credentials_file.write_text(
            json.dumps(
                {
                    "coordinator_url": self.config.coordinator_url.rstrip("/"),
                    "worker_id": worker_id,
                    "token": token,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        try:
            self.config.credentials_file.chmod(0o600)
        except OSError:
            pass

    def connect(self) -> None:
        credentials = self._load_credentials()
        if credentials:
            self.worker_id, self.worker_token = credentials
            try:
                self.client.heartbeat(self.worker_id, self.worker_token, [])
            except CoordinatorError as exc:
                if exc.status not in {401, 403, 404} or not self.config.pair_key:
                    raise
                self.worker_id, self.worker_token = "", ""
            else:
                self._refresh_profile()
                logger.info("Connected as existing worker %s", self.worker_id)
                return
        if self.config.pair_key:
            registration = self.client.pair(
                pair_key=self.config.pair_key,
                name=self.config.name,
                worker_id=self.config.worker_id,
                concurrency=self.config.concurrency,
                output_dir=self.config.output_dir,
            )
        else:
            raise RuntimeError("ATEABLE_PAIR_KEY is required for first pairing")
        self.worker_id = registration["id"]
        self.worker_token = registration["token"]
        self._save_credentials(self.worker_id, self.worker_token)
        self._refresh_profile()
        logger.info("Registered remote worker %s", self.worker_id)

    def _refresh_profile(self) -> None:
        try:
            self.client.profile(
                self.worker_id,
                self.worker_token,
                {
                    "name": self.config.name,
                    "max_concurrency": self.config.concurrency,
                    "capabilities": [
                        "original",
                        "wav",
                        "mp3",
                        "m4a",
                        "flac",
                        "opus",
                        "mp4",
                    ],
                    "output_dir": str(self.config.output_dir.resolve()),
                },
            )
        except CoordinatorError as exc:
            if exc.status != 404:
                raise

    def _download(self, job: dict[str, Any]) -> None:
        if self.config.network_mode == "tor":
            selected_proxy = effective_proxy("tor", None, self.config.tor_proxy)
        elif self.config.network_mode == "proxy":
            selected_proxy = effective_proxy("proxy", self.config.proxy)
        else:
            selected_proxy = job.get("proxy")
        spec = DownloadSpec(
            job_id=job["id"],
            url=job["canonical_url"],
            media_format=job["format"],
            output_subdir=job.get("output_subdir"),
            proxy=selected_proxy,
            cookies_file=self.config.cookies_file or job.get("cookies_file"),
            network_mode=self.config.network_mode,
        )

        def progress(update: dict[str, Any]) -> None:
            body = {key: value for key, value in update.items() if value is not None}
            if isinstance(body.get("title"), str):
                body["title"] = body["title"][:1000]
            try:
                self.client.progress(self.worker_id, self.worker_token, job["id"], body)
            except CoordinatorError as exc:
                if exc.status in {404, 409}:
                    raise DownloadStopped(str(exc)) from exc
                raise DownloadFailure(str(exc), retryable=True) from exc

        try:
            if job.get("kind") == "discover":
                from .object_storage import S3Source

                options = job.get("task_options") or {}
                discovery = S3Source(
                    network_mode=spec.network_mode, proxy=spec.proxy
                ).discover(
                    spec.url,
                    limit=options.get("limit", 5000),
                    continuation_token=options.get("continuation_token"),
                    progress=progress,
                )
                try:
                    self.client.discover(
                        self.worker_id,
                        self.worker_token,
                        job["id"],
                        discovery.items,
                        discovery.continuation_token,
                    )
                except CoordinatorError as exc:
                    if exc.status in {404, 409}:
                        raise DownloadStopped(
                            "Discovery job was stopped or completed"
                        ) from exc
                    raise DownloadFailure(
                        "Discovery completion could not be reported",
                        retryable=exc.status is None or exc.status >= 500,
                    ) from exc
                return
            result = self.downloader.download(spec, self.config.output_dir, progress)
            try:
                size_bytes = Path(result.output_path).stat().st_size
            except OSError:
                size_bytes = None
            try:
                self.inventory.record(job["id"], result.output_path)
            except (OSError, ValueError) as exc:
                logger.warning(
                    "Download succeeded; local inventory could not be saved: %s", exc
                )
            self.client.complete(
                self.worker_id,
                self.worker_token,
                job["id"],
                result.output_path,
                result.title,
                size_bytes,
            )
        except DownloadStopped:
            logger.info("Job %s was stopped by the coordinator", job["id"])
        except DownloadFailure as exc:
            logger.warning("Job %s failed: %s", job["id"], exc)
            try:
                self.client.fail(
                    self.worker_id,
                    self.worker_token,
                    job["id"],
                    str(exc),
                    exc.retryable,
                    exc.failure_kind,
                    exc.cooldown_seconds,
                )
            except CoordinatorError:
                logger.exception("Could not report failed job %s", job["id"])
        except Exception as exc:
            logger.exception("Unexpected failure for job %s", job["id"])
            try:
                self.client.fail(
                    self.worker_id,
                    self.worker_token,
                    job["id"],
                    f"{exc.__class__.__name__}: {exc}",
                    True,
                )
            except CoordinatorError:
                logger.exception("Could not report failed job %s", job["id"])

    def run(self) -> None:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        self.connect()
        failures = 0
        tor_ready = self.config.network_mode != "tor"
        next_tor_probe = 0.0
        next_inventory = 0.0
        try:
            while True:
                for job_id, future in list(self._futures.items()):
                    if future.done():
                        self._futures.pop(job_id)
                        try:
                            future.result()
                        except Exception:
                            logger.exception("Worker task %s crashed", job_id)
                try:
                    active = list(self._futures)
                    self.client.heartbeat(self.worker_id, self.worker_token, active)
                    now = time.monotonic()
                    if (
                        self._inventory_future is not None
                        and self._inventory_future.done()
                    ):
                        try:
                            self.client.inventory(
                                self.worker_id,
                                self.worker_token,
                                self._inventory_future.result(),
                            )
                        except (OSError, CoordinatorError) as exc:
                            logger.warning("Storage inventory unavailable: %s", exc)
                        self._inventory_future = None
                    if now >= next_inventory and self._inventory_future is None:
                        self._inventory_future = self._inventory_pool.submit(
                            self.inventory.snapshot
                        )
                        next_inventory = now + 30
                    if self.config.network_mode == "tor" and now >= next_tor_probe:
                        tor_status = probe_network(
                            "tor", tor_proxy=self.config.tor_proxy, timeout=10
                        )
                        tor_ready = tor_status.reachable
                        next_tor_probe = now + (60 if tor_ready else 10)
                        if not tor_ready:
                            logger.error(
                                "%s; refusing direct fallback", tor_status.message
                            )
                    if not tor_ready:
                        time.sleep(min(10.0, self.config.poll_interval * 5))
                        continue
                    capacity = self.config.concurrency - len(active)
                    if capacity > 0:
                        for job in self.client.claim(
                            self.worker_id, self.worker_token, capacity
                        ):
                            self._futures[job["id"]] = self._pool.submit(
                                self._download, job
                            )
                    failures = 0
                    time.sleep(self.config.poll_interval)
                except CoordinatorError as exc:
                    failures += 1
                    delay = min(30.0, 2 ** min(failures, 5)) + random.random()
                    logger.warning("%s; retrying in %.1fs", exc, delay)
                    time.sleep(delay)
        except KeyboardInterrupt:
            logger.info("Stopping worker; waiting for active downloads")
        finally:
            self._pool.shutdown(wait=True)
            self._inventory_pool.shutdown(wait=False, cancel_futures=True)


def _env_int(name: str, default: int) -> int:
    return max(1, int(os.getenv(name, str(default))))


def parse_args() -> WorkerConfig:
    parser = argparse.ArgumentParser(description="Ateable remote download worker")
    parser.add_argument(
        "--coordinator",
        default=os.getenv("ATEABLE_COORDINATOR_URL"),
    )
    parser.add_argument(
        "--name",
        default=os.getenv("ATEABLE_WORKER_NAME", socket.gethostname()),
    )
    parser.add_argument("--pair-key", default=os.getenv("ATEABLE_PAIR_KEY"))
    parser.add_argument("--worker-id", default=os.getenv("ATEABLE_WORKER_ID"))
    parser.add_argument("--worker-token", default=os.getenv("ATEABLE_WORKER_TOKEN"))
    parser.add_argument(
        "--output-dir", default=os.getenv("ATEABLE_OUTPUT_DIR", "./downloads")
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=_env_int("ATEABLE_WORKER_CONCURRENCY", 1),
    )
    parser.add_argument(
        "--credentials-file",
        default=os.getenv(
            "ATEABLE_WORKER_CREDENTIALS", "./data/worker-credentials.json"
        ),
    )
    parser.add_argument("--proxy", default=os.getenv("ATEABLE_PROXY"))
    parser.add_argument(
        "--network-mode",
        choices=["direct", "proxy", "tor"],
        default=os.getenv("ATEABLE_NETWORK_MODE", "tor").lower(),
    )
    parser.add_argument(
        "--tor-proxy",
        default=os.getenv("ATEABLE_TOR_PROXY", DEFAULT_TOR_PROXY),
    )
    parser.add_argument("--cookies-file", default=os.getenv("ATEABLE_COOKIES_FILE"))
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=float(os.getenv("ATEABLE_POLL_INTERVAL", "1")),
    )
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 64:
        parser.error("--concurrency must be between 1 and 64")
    credentials_file = Path(args.credentials_file).expanduser().resolve()
    coordinator = args.coordinator
    if args.pair_key:
        try:
            coordinator = decode_pair_key(args.pair_key).controller_url
        except ValueError as exc:
            parser.error(str(exc))
    elif not coordinator:
        try:
            stored = json.loads(credentials_file.read_text(encoding="utf-8"))
            coordinator = stored.get("coordinator_url")
        except (OSError, ValueError, TypeError):
            pass
    coordinator = coordinator or "http://127.0.0.1:8765"
    try:
        effective_proxy(args.network_mode, args.proxy, args.tor_proxy)
    except ValueError as exc:
        parser.error(str(exc))
    return WorkerConfig(
        coordinator_url=coordinator,
        name=args.name,
        pair_key=args.pair_key,
        worker_id=args.worker_id,
        worker_token=args.worker_token,
        output_dir=Path(args.output_dir).expanduser().resolve(),
        concurrency=args.concurrency,
        credentials_file=credentials_file,
        proxy=args.proxy,
        network_mode=args.network_mode,
        tor_proxy=args.tor_proxy,
        cookies_file=args.cookies_file,
        poll_interval=max(0.2, args.poll_interval),
    )


def main() -> None:
    logging.basicConfig(
        level=os.getenv("ATEABLE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from .tor_manager import ensure_tor

    config = parse_args()
    if config.network_mode == "tor":
        ensure_tor(config.tor_proxy)
    RemoteWorker(config).run()


if __name__ == "__main__":
    main()
