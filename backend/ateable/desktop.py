"""Native Qt desktop client for the ATEABLE download controller.

The HTTP client and parsing helpers intentionally use only the Python standard
library so they can be tested on machines that do not have PySide6 installed.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import sys
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from http.client import HTTPResponse
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request
from uuid import uuid4

from ateable.network import control_request_opener
from ateable.validation import normalize_download_url

APP_NAME = "ATEABLE"
DEFAULT_API_URL = "http://127.0.0.1:8765"
SUPPORTED_FORMATS = ("original", "wav", "mp3", "m4a", "flac", "opus", "mp4")
AUTO_TARGET_LABEL = "Automatic · online workers"
ACTIVE_JOB_STATUSES = {"queued", "downloading"}
TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled"}
JOB_STATUSES = (
    "queued",
    "downloading",
    "paused",
    "completed",
    "failed",
    "cancelled",
)


try:
    APP_VERSION = version("ateable-downloader")
except PackageNotFoundError:  # source checkout without installed package metadata
    APP_VERSION = "0.1.0"


try:
    import uvicorn

    from ateable.api import create_app as create_controller_app
    from ateable.config import AppConfig
    from ateable.database import Database
    from ateable.device_config import (
        DeviceConfig,
        default_config_path,
    )
    from ateable.tor_manager import ensure_tor

    BACKEND_AVAILABLE = True
    BACKEND_IMPORT_ERROR: Exception | None = None
except ImportError as exc:  # remote client mode can still operate without backend
    BACKEND_AVAILABLE = False
    BACKEND_IMPORT_ERROR = exc
    uvicorn = None
    AppConfig = None
    Database = None
    DeviceConfig = None
    default_config_path = None
    create_controller_app = None
    ensure_tor = None


def is_loopback_controller(value: str) -> bool:
    """Whether a URL can safely be owned by this desktop process."""

    try:
        parsed = urlparse(normalize_base_url(value))
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    if parsed.scheme != "http" or parsed.path not in {"", "/"}:
        return False
    if host == "localhost":
        return True
    try:
        return bool(ipaddress.ip_address(host).is_loopback)
    except ValueError:
        return False


def controller_bind_address(value: str) -> tuple[str, int]:
    parsed = urlparse(normalize_base_url(value))
    if not parsed.hostname:
        raise ValueError("Controller URL has no host.")
    default_port = 443 if parsed.scheme == "https" else 80
    return parsed.hostname, parsed.port or default_port


def controller_is_listening(value: str, timeout: float = 0.25) -> bool:
    """Use a TCP probe so an occupied controller port is never stolen."""

    try:
        host, port = controller_bind_address(value)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def should_manage_local_controller(device: Any) -> bool:
    return bool(
        BACKEND_AVAILABLE
        and device
        and getattr(device, "role", None) in {"controller", "both"}
        and is_loopback_controller(getattr(device, "controller_url", ""))
    )


def controller_app_config(device: Any) -> Any:
    """Build the backend AppConfig without weakening DeviceConfig networking."""

    if not BACKEND_AVAILABLE or AppConfig is None:
        raise RuntimeError(f"ATEABLE backend is unavailable: {BACKEND_IMPORT_ERROR}")
    host, port = controller_bind_address(device.controller_url)
    base = AppConfig.from_env()
    return base.with_overrides(
        host=host,
        port=port,
        controller_url=normalize_base_url(device.controller_url),
        output_dir=Path(device.output_dir).expanduser().resolve(),
        admin_key=device.admin_key or base.admin_key,
        embedded_worker=False,
        network_mode=device.network_mode,
        proxy=device.proxy,
        tor_proxy=device.tor_proxy,
    )


def initialize_local_device(
    device: Any,
    *,
    save_path: Path | None = None,
    expires_in_seconds: int = 600,
) -> tuple[Any, Any]:
    """Ensure a local controller has an admin key and one pairing invitation."""

    if not should_manage_local_controller(device) or Database is None:
        raise RuntimeError("Device is not configured as a local controller.")
    app_config = controller_app_config(device)
    database = Database(app_config)
    database.initialize()
    pair_key = device.last_pair_key
    if not pair_key:
        pair = database.create_pairing_key(
            controller_url=device.controller_url,
            expires_in_seconds=expires_in_seconds,
        )
        pair_key = pair["pair_key"]
    updated = device.updated(
        admin_key=database.admin_key,
        last_pair_key=pair_key,
    )
    updated.save(save_path)
    return updated, controller_app_config(updated)


class InProcessController:
    """Owns a loopback Uvicorn server and its clean desktop lifecycle."""

    def __init__(
        self,
        app_config: Any,
        *,
        tor_ensurer: Callable[[str], Any] | None = None,
    ) -> None:
        self.app_config = app_config
        self.tor_ensurer = tor_ensurer or ensure_tor
        self.server: Any | None = None
        self.thread: threading.Thread | None = None
        self.tor_thread: threading.Thread | None = None
        self.error: str | None = None
        self.tor_error: str | None = None
        self._stopping = threading.Event()

    def start(self) -> bool:
        if self.thread and self.thread.is_alive():
            return True
        if controller_is_listening(self.app_config.controller_url):
            return False
        self.thread = threading.Thread(
            target=self._run,
            name="ateable-desktop-controller",
            daemon=True,
        )
        self.thread.start()
        return True

    def _run(self) -> None:
        try:
            if (
                not BACKEND_AVAILABLE
                or uvicorn is None
                or create_controller_app is None
            ):
                raise RuntimeError(
                    f"ATEABLE backend is unavailable: {BACKEND_IMPORT_ERROR}"
                )
            if (
                self.app_config.embedded_worker
                and self.app_config.network_mode == "tor"
            ):
                # This method already runs off the UI thread. Gate the controller
                # and embedded worker on a verified Tor circuit so strict mode is
                # fail-closed from the first claimed job.
                self._start_managed_tor()
                if self._stopping.is_set():
                    return
                if self.tor_error:
                    raise RuntimeError(self.tor_error)
            if self._stopping.is_set():
                return
            app = create_controller_app(self.app_config)
            config = uvicorn.Config(
                app,
                host=self.app_config.host,
                port=self.app_config.port,
                log_level="warning",
                access_log=False,
                # Windows GUI bundles have no stdout/stderr. Uvicorn's default
                # colour formatter probes those streams while configuring logs.
                log_config=None,
            )
            self.server = uvicorn.Server(config)
            if self._stopping.is_set():
                self.server.should_exit = True
            self.server.run()
        except Exception as exc:  # noqa: BLE001 - surfaced through the native UI
            if not self._stopping.is_set():
                self.error = str(exc)

    def _start_managed_tor(self) -> None:
        try:
            if self.tor_ensurer is None:
                raise RuntimeError("ATEABLE Tor support is unavailable.")
            if self.tor_ensurer is ensure_tor:
                self.tor_ensurer(
                    self.app_config.tor_proxy,
                    cancel_event=self._stopping,
                )
            else:
                # Test and integrator callbacks keep the original one-argument
                # contract; the stop check immediately after this call still
                # prevents a late callback from ever opening the listener.
                self.tor_ensurer(self.app_config.tor_proxy)
        except Exception as exc:  # noqa: BLE001 - downloader remains fail-closed
            if not self._stopping.is_set():
                self.tor_error = str(exc)

    def stop(self, timeout: float = 5.0) -> bool:
        self._stopping.set()
        if self.server is not None:
            self.server.should_exit = True
        if (
            self.thread
            and self.thread.is_alive()
            and self.thread is not threading.current_thread()
        ):
            self.thread.join(timeout=timeout)
            if self.thread.is_alive() and self.server is not None:
                self.server.force_exit = True
                self.thread.join(timeout=1.0)
        return not bool(self.thread and self.thread.is_alive())


class Routes:
    """Controller paths, kept in one place for straightforward API changes."""

    HEALTH = "/api/health"
    BATCHES = "/api/batches"
    JOBS = "/api/jobs"
    JOB_ACTIONS = "/api/jobs/actions"
    CLEAR_COMPLETED = "/api/jobs/clear-completed"
    STATS = "/api/stats"
    WORKERS = "/api/workers"
    SETTINGS = "/api/settings"
    PAIRING_KEYS = "/api/pairing-keys"
    NETWORK_PROBE = "/api/network/probe"

    @staticmethod
    def job_action(job_id: str, action: str) -> str:
        return f"/api/jobs/{quote(job_id, safe='')}/{quote(action, safe='')}"


class ControllerError(RuntimeError):
    """Readable error returned by the controller or transport."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ParsedUrls:
    urls: tuple[str, ...]
    invalid: tuple[str, ...]
    duplicates: tuple[str, ...]


def normalize_base_url(value: str) -> str:
    """Return a clean HTTP(S) controller URL or raise ValueError."""

    candidate = value.strip()
    if not candidate:
        raise ValueError("Controller URL is required.")
    if "://" not in candidate:
        candidate = f"http://{candidate}"
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Controller URL must use http:// or https://.")
    if parsed.query or parsed.fragment:
        raise ValueError("Controller URL cannot contain a query or fragment.")
    return candidate.rstrip("/")


def _normalize_url_token(token: str) -> str:
    token = token.strip()
    wrappers = {"<": ">", "[": "]", "(": ")", "{": "}", '"': '"', "'": "'"}
    if len(token) >= 2 and wrappers.get(token[0]) == token[-1]:
        token = token[1:-1]
    if token.startswith(("youtube.com/", "www.youtube.com/", "youtu.be/")):
        token = f"https://{token}"
    return token


def is_youtube_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    allowed = host in {
        "youtu.be",
        "youtube.com",
        "youtube-nocookie.com",
    } or host.endswith((".youtube.com", ".youtube-nocookie.com"))
    return allowed and bool(parsed.path or parsed.query)


def parse_urls(text: str, limit: int = 5000) -> ParsedUrls:
    """Parse and de-duplicate download URLs without altering signed query data."""

    text = "\n".join(
        line
        for line in text.lstrip("\ufeff").splitlines()
        if not line.lstrip().startswith("#")
    )
    tokens = (
        re.split(
            r"\s+|,(?=https?://|(?:www\.)?youtube\.com/|youtu\.be/)",
            text.strip(),
            flags=re.IGNORECASE,
        )
        if text.strip()
        else []
    )
    urls: list[str] = []
    invalid: list[str] = []
    duplicates: list[str] = []
    seen: set[str] = set()
    for raw in tokens:
        value = _normalize_url_token(raw)
        if not value:
            continue
        try:
            value, download_key = normalize_download_url(value)
        except ValueError:
            invalid.append(raw)
            continue
        if download_key in seen:
            duplicates.append(value)
            continue
        seen.add(download_key)
        urls.append(value)
    if len(urls) > limit:
        invalid.extend(urls[limit:])
        urls = urls[:limit]
    return ParsedUrls(tuple(urls), tuple(invalid), tuple(duplicates))


def read_url_list(path: Path, max_bytes: int = 5 * 1024 * 1024) -> str:
    """Read a bounded text list, including common Windows Unicode encodings."""
    if not path.is_file():
        raise ValueError("Choose a text file containing download links.")
    with path.open("rb") as stream:
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("The list is too large. Split it into smaller text files.")
    encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    try:
        text = data.decode(encoding)
    except UnicodeError as exc:
        raise ValueError(
            "Save the URL list as UTF-8 or UTF-16 text, then try again."
        ) from exc
    if "\x00" in text:
        raise ValueError("Choose a text URL list, not a binary file.")
    return text.strip()


def proxy_for_mode(
    mode: str,
    custom_proxy: str = "",
    tor_proxy: str = "socks5h://127.0.0.1:9050",
) -> str | None:
    """Convert the local network mode into the batch API proxy value."""

    normalized = mode.strip().lower()
    if normalized == "direct":
        return None
    if normalized == "tor":
        value = tor_proxy.strip()
        parsed = urlparse(value)
        if parsed.scheme != "socks5h" or not parsed.hostname or not parsed.port:
            raise ValueError("Tor proxy must be a socks5h://host:port URL.")
        return value
    if normalized not in {"custom", "proxy"}:
        raise ValueError(f"Unknown network mode: {mode}")
    value = custom_proxy.strip()
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https", "socks5", "socks5h"}
        or not parsed.hostname
    ):
        raise ValueError(
            "Custom proxy must use http://, https://, socks5://, or socks5h://."
        )
    return value


def progress_percent(value: Any, status: str = "") -> int:
    if status == "completed":
        return 100
    try:
        progress = float(value or 0)
    except (TypeError, ValueError):
        return 0
    if 0 <= progress <= 1:
        progress *= 100
    return max(0, min(100, round(progress)))


def format_timestamp(value: Any) -> str:
    if not value:
        return "—"
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text


def format_bytes(value: Any) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "Unknown"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or unit == "TiB":
            return f"{size:,.1f} {unit}"
        size /= 1024
    return "Unknown"


def job_status_counts(jobs: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Return stable queue counters for the desktop summary cards."""

    counts = {status: 0 for status in JOB_STATUSES}
    total = 0
    for job in jobs:
        total += 1
        status = str(job.get("status", "")).lower()
        if status in counts:
            counts[status] += 1
    return {"total": total, **counts}


def filter_jobs(
    jobs: Iterable[dict[str, Any]],
    *,
    status: str = "all",
    query: str = "",
) -> list[dict[str, Any]]:
    """Filter a fetched queue snapshot without issuing another API request."""

    normalized_status = status.strip().lower() or "all"
    needle = query.strip().casefold()
    filtered: list[dict[str, Any]] = []
    for job in jobs:
        job_status = str(job.get("status", "unknown")).lower()
        if normalized_status == "active":
            if job_status not in ACTIVE_JOB_STATUSES:
                continue
        elif normalized_status == "attention":
            if job_status not in {"paused", "failed"}:
                continue
        elif normalized_status != "all" and job_status != normalized_status:
            continue
        if needle:
            searchable = " ".join(
                str(job.get(key) or "")
                for key in (
                    "id",
                    "batch_id",
                    "title",
                    "source_url",
                    "format",
                    "status",
                    "stage",
                    "target_worker_id",
                    "claimed_by",
                    "error",
                )
            ).casefold()
            if needle not in searchable:
                continue
        filtered.append(job)
    return filtered


def pairing_key_from_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("pair_key", "pairing_key", "key", "token", "code"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        items = payload.get("items")
        if isinstance(items, list) and items:
            return pairing_key_from_payload(items[0])
    return ""


def _error_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("message") or payload.get("error")
        if isinstance(detail, str):
            return detail
        if isinstance(detail, list):
            messages = [
                str(item.get("msg", item)) if isinstance(item, dict) else str(item)
                for item in detail
            ]
            if messages:
                return "; ".join(messages)
    return fallback


class ControllerClient:
    """Small synchronous HTTP client; UI calls it from Qt worker threads."""

    def __init__(
        self,
        base_url: str = DEFAULT_API_URL,
        *,
        controller_key: str | None = None,
        timeout: float = 12.0,
        opener: Callable[..., HTTPResponse] | None = None,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.controller_key = (controller_key or "").strip()
        self.timeout = timeout
        self._opener = opener if opener is not None else control_request_opener()

    def set_base_url(self, value: str) -> None:
        self.base_url = normalize_base_url(value)

    def set_controller_key(self, value: str | None) -> None:
        self.controller_key = (value or "").strip()

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            filtered = {key: value for key, value in query.items() if value is not None}
            if filtered:
                url = f"{url}?{urlencode(filtered, doseq=True)}"
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json", "User-Agent": "ATEABLE-Desktop/1"}
        if self.controller_key:
            headers["X-Ateable-Key"] = self.controller_key
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method.upper())
        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw = response.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except HTTPError as exc:
            status_code = exc.code
            try:
                if 300 <= status_code < 400:
                    raise ControllerError(
                        "Controller redirects are blocked; configure its final URL",
                        status_code,
                    ) from None
                try:
                    raw_error = exc.read().decode("utf-8")
                    parsed_error = json.loads(raw_error) if raw_error else {}
                except (UnicodeDecodeError, json.JSONDecodeError):
                    parsed_error = {}
            finally:
                exc.close()
            fallback = f"Controller returned HTTP {status_code}."
            raise ControllerError(
                _error_message(parsed_error, fallback), status_code
            ) from exc
        except URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise ControllerError(f"Cannot reach controller: {reason}") from exc
        except TimeoutError as exc:
            raise ControllerError("Controller request timed out.") from exc
        except json.JSONDecodeError as exc:
            raise ControllerError("Controller returned invalid JSON.") from exc

    def health(self) -> dict[str, Any]:
        return self.request("GET", Routes.HEALTH) or {}

    def create_batch(
        self,
        urls: Iterable[str],
        *,
        output_format: str = "original",
        target_worker_id: str | None = None,
        output_subdir: str | None = None,
        proxy: str | None = None,
    ) -> dict[str, Any]:
        if output_format not in SUPPORTED_FORMATS:
            raise ValueError(f"Unsupported format: {output_format}")
        payload = {
            "urls": list(urls),
            "format": output_format,
            "target_worker_id": target_worker_id or None,
            "output_subdir": output_subdir or None,
            "proxy": proxy,
            "idempotency_key": str(uuid4()),
        }
        return self.request("POST", Routes.BATCHES, payload) or {}

    def list_jobs(
        self,
        *,
        limit: int = 250,
        offset: int = 0,
        status: str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        return self.request(
            "GET",
            Routes.JOBS,
            query={"limit": limit, "offset": offset, "status": status, "q": query},
        ) or {"items": [], "total": 0}

    def job_action(self, job_id: str, action: str) -> dict[str, Any]:
        if action not in {"pause", "resume", "cancel", "retry"}:
            raise ValueError(f"Unsupported job action: {action}")
        return self.request("POST", Routes.job_action(job_id, action), {}) or {}

    def bulk_action(
        self,
        action: str,
        *,
        job_ids: Iterable[str] | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"action": action}
        if job_ids:
            payload["job_ids"] = list(job_ids)
        if status:
            payload["status"] = status
        return self.request("POST", Routes.JOB_ACTIONS, payload) or {}

    def clear_completed(self) -> dict[str, Any]:
        return self.request("POST", Routes.CLEAR_COMPLETED, {}) or {}

    def stats(self) -> dict[str, Any]:
        return self.request("GET", Routes.STATS) or {}

    def list_workers(self) -> dict[str, Any]:
        return self.request("GET", Routes.WORKERS) or {"items": [], "total": 0}

    def worker_storage(self, worker_id: str) -> dict[str, Any]:
        return (
            self.request("GET", f"{Routes.WORKERS}/{quote(worker_id, safe='')}/storage")
            or {}
        )

    def get_settings(self) -> dict[str, Any]:
        return self.request("GET", Routes.SETTINGS) or {}

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        return self.request("PATCH", Routes.SETTINGS, values) or {}

    def create_pairing_key(self, expires_in_seconds: int = 600) -> Any:
        return self.request(
            "POST",
            Routes.PAIRING_KEYS,
            {"expires_in_seconds": expires_in_seconds},
        )

    def regenerate_pairing_key(self, expires_in_seconds: int = 600) -> Any:
        return self.request(
            "POST",
            f"{Routes.PAIRING_KEYS}/regenerate",
            {"expires_in_seconds": expires_in_seconds},
        )

    def probe_network(self, mode: str, proxy: str | None) -> dict[str, Any]:
        api_mode = "proxy" if mode == "custom" else mode
        return (
            self.request(
                "POST", Routes.NETWORK_PROBE, {"mode": api_mode, "proxy": proxy}
            )
            or {}
        )


try:
    from PySide6.QtCore import (
        QItemSelectionModel,
        QObject,
        QRunnable,
        QSettings,
        Qt,
        QThreadPool,
        QTimer,
        Signal,
        Slot,
    )
    from PySide6.QtGui import (
        QAction,
        QColor,
        QFont,
        QKeySequence,
        QShortcut,
        QTextCursor,
    )
    from PySide6.QtWidgets import (
        QAbstractItemView,
        QApplication,
        QCheckBox,
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QFileDialog,
        QFormLayout,
        QFrame,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMenu,
        QMessageBox,
        QPlainTextEdit,
        QProgressBar,
        QPushButton,
        QScrollArea,
        QSpinBox,
        QStackedWidget,
        QStatusBar,
        QTableWidget,
        QTableWidgetItem,
        QTabWidget,
        QToolButton,
        QVBoxLayout,
        QWidget,
    )

    PYSIDE_AVAILABLE = True
    PYSIDE_IMPORT_ERROR: Exception | None = None
except ImportError as exc:  # pragma: no cover - exercised on hosts without Qt
    PYSIDE_AVAILABLE = False
    PYSIDE_IMPORT_ERROR = exc


if PYSIDE_AVAILABLE:

    class TaskSignals(QObject):
        success = Signal(object)
        error = Signal(str)
        finished = Signal()

    class ApiTask(QRunnable):
        def __init__(self, operation: Callable[[], Any]) -> None:
            super().__init__()
            self.operation = operation
            self.signals = TaskSignals()

        @Slot()
        def run(self) -> None:
            try:
                result = self.operation()
            except Exception as exc:  # noqa: BLE001 - all task failures must reach the UI
                try:
                    self.signals.error.emit(str(exc))
                except RuntimeError:
                    # The window may have closed while a network request finished.
                    pass
            else:
                try:
                    self.signals.success.emit(result)
                except RuntimeError:
                    pass
            finally:
                try:
                    self.signals.finished.emit()
                except RuntimeError:
                    pass

    class AteableWindow(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle(APP_NAME)
            from .branding import app_icon

            self.setWindowIcon(app_icon())
            self.resize(1180, 760)
            self.setMinimumSize(840, 560)

            self.local_settings = QSettings("ATEABLE", "ATEABLE")
            self.device_config: Any | None = None
            self.controller_runtime: InProcessController | None = None
            self._bootstrap_error: str | None = None
            if BACKEND_AVAILABLE and DeviceConfig is not None:
                try:
                    self.device_config = DeviceConfig.load()
                except (OSError, ValueError) as exc:
                    self._bootstrap_error = str(exc)

            if self.device_config is not None:
                configured_url = str(self.device_config.controller_url)
                configured_key = str(self.device_config.admin_key or "")
                # Older desktop builds duplicated this secret into QSettings.
                # DeviceConfig is permission-restricted, so remove the legacy copy.
                self.local_settings.remove("controller_key")
                self.local_settings.remove("custom_proxy")
            else:
                configured_url = str(
                    self.local_settings.value(
                        "controller_url",
                        os.getenv("ATEABLE_API_URL", DEFAULT_API_URL),
                    )
                )
                configured_key = str(
                    self.local_settings.value(
                        "controller_key",
                        os.getenv("ATEABLE_ADMIN_KEY", ""),
                    )
                )
            try:
                configured_url = normalize_base_url(configured_url)
            except ValueError:
                configured_url = DEFAULT_API_URL

            if should_manage_local_controller(self.device_config):
                already_listening = controller_is_listening(configured_url)
                try:
                    if (
                        not self.device_config.admin_key
                        or not self.device_config.last_pair_key
                    ):
                        self.device_config, app_config = initialize_local_device(
                            self.device_config
                        )
                    else:
                        app_config = controller_app_config(self.device_config)
                    configured_url = normalize_base_url(
                        self.device_config.controller_url
                    )
                    configured_key = str(self.device_config.admin_key or "")
                    if not already_listening:
                        self.controller_runtime = InProcessController(app_config)
                        self.controller_runtime.start()
                except (OSError, RuntimeError, ValueError) as exc:
                    self._bootstrap_error = str(exc)

            self.client = ControllerClient(
                configured_url, controller_key=configured_key
            )
            self.thread_pool = QThreadPool.globalInstance()
            self._tasks: set[ApiTask] = set()
            self._connection_generation = 0
            self._jobs_loading = False
            self._devices_loading = False
            self._enqueue_in_flight = False
            self._pairing_in_flight = False
            self._probe_in_flight = False
            self._closing = False
            self._ssh_provisioner: Any | None = None
            self._ssh_in_flight = False
            self._storage_worker_id: str | None = None
            self._storage_revision = 0
            self._batch_user_opened = False
            self.jobs: list[dict[str, Any]] = []
            self.workers: list[dict[str, Any]] = []
            self._connection_state = "connecting"
            self._workers_verified = False
            self._jobs_total = 0
            self._jobs_match_total = 0
            self._jobs_offset = 0
            self._jobs_page_size = 250
            self._jobs_revision = 0
            self._jobs_loaded_revision = -1
            self._jobs_server_filtered = False
            self.job_search_timer = QTimer(self)
            self.job_search_timer.setSingleShot(True)
            self.job_search_timer.setInterval(250)
            self.job_search_timer.timeout.connect(self.refresh_jobs)
            self._job_counts = job_status_counts(())
            self.desktop_device_id = str(
                getattr(self.device_config, "device_id", "")
                or self.local_settings.value("desktop_device_id", "")
            ).strip()
            if not self.desktop_device_id:
                self.desktop_device_id = f"desktop-{uuid4().hex[:12]}"
                self.local_settings.setValue(
                    "desktop_device_id", self.desktop_device_id
                )

            self.setStatusBar(QStatusBar(self))
            self.statusBar().setSizeGripEnabled(False)
            self.statusBar().showMessage("Starting controller…")
            self.status_hint = QLabel(
                "Ctrl+Enter start downloads  ·  Ctrl+K search  ·  F5 refresh"
            )
            self.status_hint.setProperty("muted", True)
            self.statusBar().addPermanentWidget(self.status_hint)

            central = QWidget()
            central.setObjectName("appRoot")
            central_layout = QVBoxLayout(central)
            central_layout.setContentsMargins(0, 0, 0, 0)
            central_layout.setSpacing(0)
            central_layout.addWidget(self._build_app_header())

            self.tabs = QTabWidget()
            self.tabs.setObjectName("mainTabs")
            self.tabs.setDocumentMode(True)
            central_layout.addWidget(self.tabs, 1)
            self.setCentralWidget(central)

            self.queue_tab = self._build_queue_tab()
            from .buckets_ui import BucketsPanel

            self.buckets_tab = BucketsPanel(self)
            from .workspaces_ui import WorkspacesPanel

            self.workspaces_tab = WorkspacesPanel(self)
            self.devices_tab = self._build_devices_tab()
            self.settings_tab = self._build_settings_tab(configured_url, configured_key)
            from .getting_started_ui import GettingStartedPanel

            self.start_tab = GettingStartedPanel(self)
            self.start_tab.navigate.connect(self._start_action)
            self._tab_names = {
                "start": self.start_tab,
                "queue": self.queue_tab,
                "buckets": self.buckets_tab,
                "workspaces": self.workspaces_tab,
                "devices": self.devices_tab,
                "settings": self.settings_tab,
            }
            self.tabs.addTab(self.start_tab, "&Start")
            self.tabs.addTab(self.queue_tab, "&Queue")
            self.tabs.addTab(self.buckets_tab, "&Buckets")
            self.tabs.addTab(self.workspaces_tab, "&Workspaces")
            self.tabs.addTab(self.devices_tab, "&Devices")
            self.tabs.addTab(self.settings_tab, "&Settings")
            self.tabs.currentChanged.connect(self._tab_changed)
            self.tabs.setCurrentWidget(self._saved_tab())

            self._install_actions()
            self._apply_style()

            saved_geometry = self.local_settings.value("window_geometry")
            if saved_geometry:
                self.restoreGeometry(saved_geometry)

            self.poll_timer = QTimer(self)
            self.poll_timer.setInterval(3000)
            self.poll_timer.timeout.connect(self._poll_current_tab)
            self.poll_timer.start()
            if self._bootstrap_error:
                self.statusBar().showMessage(self._bootstrap_error, 15000)
                self._set_connection_state("error", "Startup issue")
            startup_delay_ms = 400 if self.controller_runtime is not None else 0
            QTimer.singleShot(startup_delay_ms, self.refresh_all)

        def _build_app_header(self) -> QWidget:
            header = QFrame()
            header.setObjectName("appHeader")
            layout = QHBoxLayout(header)
            layout.setContentsMargins(20, 13, 20, 13)
            layout.setSpacing(12)

            brand = QVBoxLayout()
            brand.setSpacing(1)
            from .branding import logo_pixmap

            logo = QLabel()
            logo.setPixmap(logo_pixmap(52))
            logo.setFixedSize(52, 52)
            logo.setAccessibleName("Ateable logo")
            layout.addWidget(logo)
            title = QLabel("Ateable Admin")
            title.setObjectName("appTitle")
            subtitle = QLabel("Bulk data collection. Your computers. Your control.")
            subtitle.setObjectName("appSubtitle")
            brand.addWidget(title)
            brand.addWidget(subtitle)
            layout.addLayout(brand)
            layout.addStretch(1)

            version_label = QLabel(f"v{APP_VERSION}")
            version_label.setObjectName("versionLabel")
            layout.addWidget(version_label)
            self.connection_pill = QLabel("●  Connecting")
            self.connection_pill.setObjectName("connectionPill")
            self.connection_pill.setProperty("state", "connecting")
            self.connection_pill.setToolTip(self.client.base_url)
            layout.addWidget(self.connection_pill)
            return header

        @staticmethod
        def _repolish(widget: QWidget) -> None:
            widget.style().unpolish(widget)
            widget.style().polish(widget)
            widget.update()

        def _set_connection_state(self, state: str, detail: str = "") -> None:
            self._connection_state = state
            if state != "online":
                self._workers_verified = False
            labels = {
                "online": "●  Controller online",
                "connecting": "●  Connecting",
                "error": "●  Connection issue",
            }
            self.connection_pill.setText(labels.get(state, labels["connecting"]))
            self.connection_pill.setProperty("state", state)
            tooltip = self.client.base_url
            if detail:
                tooltip = f"{tooltip}\n{detail}"
            self.connection_pill.setToolTip(tooltip)
            self._repolish(self.connection_pill)
            self._update_start_status()
            if state == "online" and self.statusBar().currentMessage().startswith(
                ("Starting", "Preparing")
            ):
                self.statusBar().showMessage("Ready", 3000)

        def _saved_tab(self) -> QWidget:
            name = self.local_settings.value("active_tab_name")
            if isinstance(name, str) and name in self._tab_names:
                return self._tab_names[name]
            # Previous builds stored a zero-based index in the five-tab layout.
            legacy = self.local_settings.value("active_tab")
            if legacy is not None:
                try:
                    old_names = (
                        "queue",
                        "buckets",
                        "workspaces",
                        "devices",
                        "settings",
                    )
                    index = int(legacy)
                    if 0 <= index < len(old_names):
                        return self._tab_names[old_names[index]]
                except (TypeError, ValueError):
                    pass
            return self.start_tab

        def _update_start_status(self) -> None:
            if not hasattr(self, "start_tab"):
                return
            self.start_tab.update_status(
                self._connection_state,
                self.workers,
                workers_verified=self._workers_verified,
                jobs_total=self._jobs_total,
            )
            self.ssh_pair_button.setEnabled(
                self._connection_state == "online" and not self._ssh_in_flight
            )

        def _start_action(self, target: str) -> None:
            if target == "connect":
                if self._connection_state == "online" and not self._ssh_in_flight:
                    self.show_ssh_pair_dialog()
                return
            if target == "download":
                self._show_batch_composer()
            elif target == "retry":
                self.refresh_all()
            elif target in self._tab_names:
                self.tabs.setCurrentWidget(self._tab_names[target])

        def _local_controller_starting(self) -> bool:
            runtime = self.controller_runtime
            return bool(
                runtime is not None
                and runtime.thread is not None
                and runtime.thread.is_alive()
                and runtime.server is None
                and not runtime.error
                and not runtime.tor_error
            )

        def _stat_card(
            self, label: str, *, tone: str = "neutral"
        ) -> tuple[QFrame, QLabel]:
            card = QFrame()
            card.setObjectName("statCard")
            card.setProperty("tone", tone)
            layout = QVBoxLayout(card)
            layout.setContentsMargins(13, 9, 13, 9)
            layout.setSpacing(1)
            value = QLabel("0")
            value.setObjectName("statValue")
            caption = QLabel(label)
            caption.setObjectName("statLabel")
            layout.addWidget(value)
            layout.addWidget(caption)
            return card, value

        def _empty_state(
            self,
            title: str,
            body: str,
            action_text: str,
            action: Callable[[], None],
        ) -> tuple[QWidget, QLabel, QLabel]:
            state = QFrame()
            state.setObjectName("emptyState")
            outer = QVBoxLayout(state)
            outer.setContentsMargins(18, 14, 18, 14)
            outer.setSpacing(5)
            outer.addStretch(1)
            title_label = QLabel(title)
            title_label.setObjectName("emptyTitle")
            title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            body_label = QLabel(body)
            body_label.setObjectName("emptyBody")
            body_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            body_label.setWordWrap(True)
            button = QPushButton(action_text)
            button.clicked.connect(action)
            button.setMaximumWidth(180)
            button.setProperty("quiet", True)
            outer.addWidget(title_label)
            outer.addWidget(body_label)
            outer.addSpacing(3)
            outer.addWidget(button, alignment=Qt.AlignmentFlag.AlignHCenter)
            outer.addStretch(1)
            return state, title_label, body_label

        def _build_queue_tab(self) -> QWidget:
            tab = QWidget()
            layout = QVBoxLayout(tab)
            layout.setContentsMargins(18, 16, 18, 16)
            layout.setSpacing(12)

            batch_group = QGroupBox("Download links")
            self.batch_group = batch_group
            batch_group.setMinimumHeight(242)
            batch_layout = QVBoxLayout(batch_group)
            batch_layout.setSpacing(8)
            batch_help = QLabel(
                "Paste up to 5,000 dataset, file, media, or S3 links. Download batches on the computer you choose."
            )
            batch_help.setProperty("muted", True)
            batch_help.setWordWrap(True)
            batch_header = QHBoxLayout()
            batch_header.addWidget(batch_help, 1)
            self.import_urls_button = QPushButton("Import list…")
            self.import_urls_button.setToolTip("Add links from a text file (Ctrl+O)")
            self.import_urls_button.setAccessibleName("Import URL list")
            self.import_urls_button.clicked.connect(self.import_url_list)
            batch_header.addWidget(self.import_urls_button)
            self.hide_batch_button = QToolButton()
            self.hide_batch_button.setText("Hide")
            self.hide_batch_button.setToolTip("Collapse the batch composer")
            self.hide_batch_button.clicked.connect(self._hide_batch_composer)
            self.hide_batch_button.setVisible(False)
            batch_header.addWidget(self.hide_batch_button)
            batch_layout.addLayout(batch_header)
            url_label = QLabel("&Download URLs")
            self.url_input = QPlainTextEdit()
            url_label.setBuddy(self.url_input)
            self.url_input.setAccessibleName("Download URLs")
            self.url_input.setAccessibleDescription(
                "One or more HTTP(S) download URLs separated by spaces, commas, or new lines"
            )
            self.url_input.setPlaceholderText(
                "Paste links here — one per line\nhttps://example.com/file.zip"
            )
            self.url_input.setMinimumHeight(64)
            self.url_input.setMaximumHeight(92)
            self.url_input.setTabChangesFocus(True)
            self.url_input.textChanged.connect(self._update_url_count)
            batch_layout.addWidget(url_label)
            batch_layout.addWidget(self.url_input)

            options = QGridLayout()
            options.setHorizontalSpacing(10)
            options.addWidget(QLabel("Format"), 0, 0)
            self.format_combo = QComboBox()
            for output_format in SUPPORTED_FORMATS:
                self.format_combo.addItem(
                    "Keep original file"
                    if output_format == "original"
                    else output_format.upper(),
                    output_format,
                )
            self.format_combo.setCurrentIndex(0)
            options.addWidget(self.format_combo, 1, 0)

            options.addWidget(QLabel("Download computer"), 0, 1)
            self.target_combo = QComboBox()
            self.target_combo.setEditable(True)
            self.target_combo.addItem(AUTO_TARGET_LABEL, None)
            self.target_combo.setMinimumWidth(220)
            self.target_combo.setToolTip(
                "Choose an online remote worker, or distribute automatically. The controller never downloads files."
            )
            options.addWidget(self.target_combo, 1, 1)

            options.addWidget(QLabel("Output subfolder"), 0, 2)
            self.output_subdir = QLineEdit()
            self.output_subdir.setPlaceholderText("Optional")
            options.addWidget(self.output_subdir, 1, 2)

            self.enqueue_button = QPushButton("&Start downloads")
            self.enqueue_button.setProperty("primary", True)
            self.enqueue_button.setEnabled(False)
            self.enqueue_button.setToolTip(
                "Download these links on the selected computer (Ctrl+Enter)"
            )
            self.enqueue_button.clicked.connect(self.enqueue_urls)
            options.addWidget(self.enqueue_button, 1, 3)
            options.setColumnStretch(1, 1)
            options.setColumnStretch(2, 1)
            batch_layout.addLayout(options)

            feedback = QHBoxLayout()
            self.url_count = QLabel("0 valid URLs")
            self.url_count.setProperty("muted", True)
            feedback.addWidget(self.url_count)
            feedback.addStretch(1)
            shortcut_hint = QLabel("Ctrl+Enter to start downloads")
            shortcut_hint.setProperty("muted", True)
            feedback.addWidget(shortcut_hint)
            batch_layout.addLayout(feedback)
            layout.addWidget(batch_group)

            self.queue_stats = QWidget()
            stats = QHBoxLayout(self.queue_stats)
            stats.setContentsMargins(0, 0, 0, 0)
            stats.setSpacing(8)
            total_card, self.total_jobs_value = self._stat_card("Total jobs")
            active_card, self.active_jobs_value = self._stat_card(
                "Active", tone="active"
            )
            done_card, self.completed_jobs_value = self._stat_card(
                "Completed", tone="success"
            )
            attention_card, self.attention_jobs_value = self._stat_card(
                "Needs attention", tone="warning"
            )
            for card in (total_card, active_card, done_card, attention_card):
                stats.addWidget(card, 1)
            self.queue_stats.setVisible(False)
            layout.addWidget(self.queue_stats)

            self.queue_filter_bar = QWidget()
            toolbar = QHBoxLayout(self.queue_filter_bar)
            toolbar.setContentsMargins(0, 0, 0, 0)
            toolbar.setSpacing(7)
            self.queue_summary = QLabel("Loading queue…")
            self.queue_summary.setProperty("muted", True)
            self.queue_summary.setWordWrap(True)
            toolbar.addWidget(self.queue_summary)
            toolbar.addStretch(1)

            self.new_batch_button = QPushButton("New batch")
            self.new_batch_button.setProperty("primary", True)
            self.new_batch_button.clicked.connect(self._show_batch_composer)
            toolbar.addWidget(self.new_batch_button)

            self.job_search = QLineEdit()
            self.job_search.setPlaceholderText("Search queue")
            self.job_search.setClearButtonEnabled(True)
            self.job_search.setMaxLength(500)
            self.job_search.setMaximumWidth(210)
            self.job_search.setAccessibleName("Search queue")
            self.job_search.textChanged.connect(self._queue_filters_changed)
            toolbar.addWidget(self.job_search)

            self.job_status_filter = QComboBox()
            self.job_status_filter.setAccessibleName("Queue status filter")
            for label, value in (
                ("All statuses", "all"),
                ("Active", "active"),
                ("Needs attention", "attention"),
                ("Queued", "queued"),
                ("Downloading", "downloading"),
                ("Paused", "paused"),
                ("Completed", "completed"),
                ("Failed", "failed"),
                ("Cancelled", "cancelled"),
            ):
                self.job_status_filter.addItem(label, value)
            saved_filter = str(self.local_settings.value("queue_status_filter", "all"))
            filter_index = self.job_status_filter.findData(saved_filter)
            self.job_status_filter.setCurrentIndex(max(0, filter_index))
            self.job_status_filter.currentIndexChanged.connect(
                self._queue_filters_changed
            )
            toolbar.addWidget(self.job_status_filter)
            self.queue_filter_bar.setVisible(False)
            layout.addWidget(self.queue_filter_bar)

            self.queue_action_bar = QWidget()
            action_toolbar = QHBoxLayout(self.queue_action_bar)
            action_toolbar.setContentsMargins(0, 0, 0, 0)
            action_toolbar.setSpacing(7)
            selection_hint = QLabel("Select rows to apply a bulk action")
            selection_hint.setProperty("muted", True)
            action_toolbar.addWidget(selection_hint)
            action_toolbar.addStretch(1)

            self.selected_action = QComboBox()
            self.selected_action.setAccessibleName("Action for selected jobs")
            for label, value in (
                ("Selected action…", None),
                ("Pause selected", "pause"),
                ("Resume selected", "resume"),
                ("Cancel selected", "cancel"),
                ("Retry selected", "retry"),
            ):
                self.selected_action.addItem(label, value)
            self.selected_action.currentIndexChanged.connect(
                self._update_selected_action_state
            )
            action_toolbar.addWidget(self.selected_action)
            self.apply_selected_button = QPushButton("Apply")
            self.apply_selected_button.setEnabled(False)
            self.apply_selected_button.clicked.connect(self.apply_selected_action)
            action_toolbar.addWidget(self.apply_selected_button)

            queue_actions = QToolButton()
            queue_actions.setText("Queue actions")
            queue_actions.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            queue_menu = QMenu(queue_actions)
            self.pause_active_action = queue_menu.addAction("Pause all active jobs")
            self.pause_active_action.triggered.connect(self.pause_active)
            self.resume_paused_action = queue_menu.addAction("Resume all paused jobs")
            self.resume_paused_action.triggered.connect(
                lambda: self._bulk_status_action("resume", "paused")
            )
            queue_menu.addSeparator()
            self.clear_completed_action = queue_menu.addAction("Clear completed jobs…")
            self.clear_completed_action.triggered.connect(self.clear_completed)
            for action in (
                self.pause_active_action,
                self.resume_paused_action,
                self.clear_completed_action,
            ):
                action.setEnabled(False)
            queue_actions.setMenu(queue_menu)
            action_toolbar.addWidget(queue_actions)

            self.refresh_jobs_button = QPushButton("&Refresh")
            self.refresh_jobs_button.clicked.connect(self.refresh_jobs)
            action_toolbar.addWidget(self.refresh_jobs_button)
            self.queue_action_bar.setVisible(False)
            layout.addWidget(self.queue_action_bar)

            self.jobs_table = QTableWidget(0, 7)
            self.jobs_table.setHorizontalHeaderLabels(
                [
                    "Source / title",
                    "Format",
                    "Target",
                    "Status",
                    "Progress",
                    "Updated",
                    "Actions",
                ]
            )
            self.jobs_table.setSelectionBehavior(
                QAbstractItemView.SelectionBehavior.SelectRows
            )
            self.jobs_table.setSelectionMode(
                QAbstractItemView.SelectionMode.ExtendedSelection
            )
            self.jobs_table.setEditTriggers(
                QAbstractItemView.EditTrigger.NoEditTriggers
            )
            self.jobs_table.setAlternatingRowColors(True)
            self.jobs_table.setShowGrid(False)
            self.jobs_table.verticalHeader().setVisible(False)
            self.jobs_table.verticalHeader().setDefaultSectionSize(42)
            self.jobs_table.itemSelectionChanged.connect(
                self._update_selected_action_state
            )
            header = self.jobs_table.horizontalHeader()
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            for column in (1, 2, 3, 4, 5, 6):
                header.setSectionResizeMode(
                    column, QHeaderView.ResizeMode.ResizeToContents
                )
            self.jobs_stack = QStackedWidget()
            self.jobs_stack.addWidget(self.jobs_table)
            empty, self.jobs_empty_title, self.jobs_empty_body = self._empty_state(
                "No downloads yet",
                "Paste one or more download links above to create your first batch.",
                "Add download URLs",
                self._focus_url_input,
            )
            self.jobs_stack.addWidget(empty)
            self.jobs_stack.setCurrentIndex(1)
            layout.addWidget(self.jobs_stack, 1)
            self.queue_pager = QWidget()
            pager = QHBoxLayout(self.queue_pager)
            pager.setContentsMargins(0, 0, 0, 0)
            self.queue_page_label = QLabel("Page 1 of 1")
            self.queue_page_label.setProperty("muted", True)
            pager.addWidget(self.queue_page_label)
            pager.addStretch(1)
            self.previous_jobs_button = QPushButton("Previous")
            self.previous_jobs_button.setAccessibleName("Previous queue page")
            self.previous_jobs_button.clicked.connect(
                lambda: self._change_queue_page(-1)
            )
            self.next_jobs_button = QPushButton("Next")
            self.next_jobs_button.setAccessibleName("Next queue page")
            self.next_jobs_button.clicked.connect(lambda: self._change_queue_page(1))
            pager.addWidget(self.previous_jobs_button)
            pager.addWidget(self.next_jobs_button)
            self.previous_jobs_button.setEnabled(False)
            self.next_jobs_button.setEnabled(False)
            self.queue_pager.setVisible(False)
            layout.addWidget(self.queue_pager)
            return tab

        def _build_devices_tab(self) -> QWidget:
            tab = QWidget()
            layout = QVBoxLayout(tab)
            layout.setContentsMargins(18, 16, 18, 16)
            layout.setSpacing(12)
            top_cards = QHBoxLayout()
            top_cards.setSpacing(10)

            local_group = QGroupBox("This Admin computer")
            local_form = QFormLayout(local_group)
            self.device_name_label = QLabel(
                str(getattr(self.device_config, "name", "This machine"))
            )
            self.device_id_label = QLabel(self.desktop_device_id)
            self.device_id_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            self.device_id_label.setToolTip("Desktop device identifier")
            self.device_role_label = QLabel(
                str(getattr(self.device_config, "role", "client"))
            )
            device_id_row = QWidget()
            device_id_layout = QHBoxLayout(device_id_row)
            device_id_layout.setContentsMargins(0, 0, 0, 0)
            device_id_layout.setSpacing(6)
            device_id_layout.addWidget(self.device_id_label, 1)
            copy_device_id = QToolButton()
            copy_device_id.setText("Copy")
            copy_device_id.clicked.connect(self.copy_device_id)
            device_id_layout.addWidget(copy_device_id)
            local_form.addRow("Name", self.device_name_label)
            local_form.addRow("Device ID", device_id_row)
            local_form.addRow("Role", self.device_role_label)
            top_cards.addWidget(local_group, 1)

            pairing_group = QGroupBox("Connect a download computer")
            pairing_outer = QVBoxLayout(pairing_group)
            connect_help = QLabel(
                "Connect a Linux computer over SSH on your LAN or an existing private network. Ateable installs its worker; files use that computer's disk and internet connection."
            )
            connect_help.setWordWrap(True)
            connect_help.setProperty("muted", True)
            pairing_outer.addWidget(connect_help)
            self.ssh_pair_button = QPushButton("Connect a computer via SSH…")
            self.ssh_pair_button.setProperty("primary", True)
            self.ssh_pair_button.setEnabled(False)
            self.ssh_pair_button.clicked.connect(self.show_ssh_pair_dialog)
            pairing_outer.addWidget(
                self.ssh_pair_button, alignment=Qt.AlignmentFlag.AlignLeft
            )
            self.manual_pairing_toggle = QToolButton()
            self.manual_pairing_toggle.setText("Advanced: connect an existing worker")
            self.manual_pairing_toggle.setCheckable(True)
            self.manual_pairing_toggle.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextBesideIcon
            )
            self.manual_pairing_toggle.setArrowType(Qt.ArrowType.RightArrow)
            pairing_outer.addWidget(self.manual_pairing_toggle)
            self.manual_pairing_panel = QWidget()
            manual_layout = QVBoxLayout(self.manual_pairing_panel)
            manual_layout.setContentsMargins(0, 0, 0, 0)
            pairing_help = QLabel(
                "Already installed the worker yourself? Generate a single-use pairing key and enter it on that computer. Keys expire after 10 minutes."
            )
            pairing_help.setProperty("muted", True)
            pairing_help.setWordWrap(True)
            manual_layout.addWidget(pairing_help)
            pairing_layout = QHBoxLayout()
            self.pairing_key = QLineEdit()
            self.pairing_key.setReadOnly(True)
            self.pairing_key.setEchoMode(QLineEdit.EchoMode.Password)
            self.pairing_key.setPlaceholderText("No pairing key available")
            self.pairing_key.setText(
                str(getattr(self.device_config, "last_pair_key", "") or "")
            )
            mono = QFont("Monospace")
            mono.setStyleHint(QFont.StyleHint.Monospace)
            self.pairing_key.setFont(mono)
            pairing_layout.addWidget(self.pairing_key, 1)
            self.pairing_reveal = QToolButton()
            self.pairing_reveal.setText("Show")
            self.pairing_reveal.setCheckable(True)
            self.pairing_reveal.toggled.connect(self._toggle_pairing_key)
            pairing_layout.addWidget(self.pairing_reveal)
            copy_button = QPushButton("&Copy")
            copy_button.clicked.connect(self.copy_pairing_key)
            pairing_layout.addWidget(copy_button)
            self.regenerate_pairing_button = QPushButton("&Regenerate…")
            self.regenerate_pairing_button.clicked.connect(self.regenerate_pairing_key)
            pairing_layout.addWidget(self.regenerate_pairing_button)
            manual_layout.addLayout(pairing_layout)
            pairing_outer.addWidget(self.manual_pairing_panel)
            self.manual_pairing_panel.setVisible(False)

            def toggle_manual_pairing(expanded: bool) -> None:
                self.manual_pairing_panel.setVisible(expanded)
                self.manual_pairing_toggle.setArrowType(
                    Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
                )

            self.manual_pairing_toggle.toggled.connect(toggle_manual_pairing)
            top_cards.addWidget(pairing_group, 2)
            layout.addLayout(top_cards)

            device_toolbar = QHBoxLayout()
            self.devices_summary = QLabel("Loading download computers…")
            self.devices_summary.setProperty("muted", True)
            device_toolbar.addWidget(self.devices_summary)
            device_toolbar.addStretch(1)
            self.refresh_devices_button = QPushButton("&Refresh")
            self.refresh_devices_button.clicked.connect(self.refresh_devices)
            device_toolbar.addWidget(self.refresh_devices_button)
            layout.addLayout(device_toolbar)

            device_stats = QHBoxLayout()
            device_stats.setSpacing(8)
            online_card, self.online_workers_value = self._stat_card(
                "Online", tone="success"
            )
            offline_card, self.offline_workers_value = self._stat_card(
                "Offline", tone="warning"
            )
            capacity_card, self.worker_capacity_value = self._stat_card(
                "Total capacity", tone="active"
            )
            for card in (online_card, offline_card, capacity_card):
                device_stats.addWidget(card, 1)
            layout.addLayout(device_stats)

            self.devices_table = QTableWidget(0, 5)
            self.devices_table.setHorizontalHeaderLabels(
                ["Name", "Device ID", "Status", "Capacity", "Last seen"]
            )
            self.devices_table.setEditTriggers(
                QAbstractItemView.EditTrigger.NoEditTriggers
            )
            self.devices_table.setSelectionBehavior(
                QAbstractItemView.SelectionBehavior.SelectRows
            )
            self.devices_table.setSelectionMode(
                QAbstractItemView.SelectionMode.SingleSelection
            )
            self.devices_table.itemSelectionChanged.connect(self.refresh_worker_storage)
            self.devices_table.setAlternatingRowColors(True)
            self.devices_table.setShowGrid(False)
            self.devices_table.verticalHeader().setVisible(False)
            self.devices_table.verticalHeader().setDefaultSectionSize(42)
            header = self.devices_table.horizontalHeader()
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            for column in (2, 3, 4):
                header.setSectionResizeMode(
                    column, QHeaderView.ResizeMode.ResizeToContents
                )
            self.devices_stack = QStackedWidget()
            self.devices_stack.addWidget(self.devices_table)
            empty, self.devices_empty_title, self.devices_empty_body = (
                self._empty_state(
                    "Connect your first download computer",
                    "Use the SSH button above. Ateable will install and connect its worker.",
                    "Connect a computer…",
                    lambda: self._start_action("connect"),
                )
            )
            self.devices_stack.addWidget(empty)
            self.devices_stack.setCurrentIndex(1)
            layout.addWidget(self.devices_stack, 1)
            storage_group = QGroupBox("Selected worker · storage and recent files")
            storage_layout = QVBoxLayout(storage_group)
            storage_header = QHBoxLayout()
            self.worker_storage_summary = QLabel(
                "Select a worker to see its remote storage."
            )
            self.worker_storage_summary.setWordWrap(True)
            storage_header.addWidget(self.worker_storage_summary, 1)
            self.worker_storage_refresh = QPushButton("Refresh storage")
            self.worker_storage_refresh.clicked.connect(
                lambda: self.refresh_worker_storage(force=True)
            )
            storage_header.addWidget(self.worker_storage_refresh)
            storage_layout.addLayout(storage_header)
            self.worker_files = QTableWidget(0, 4)
            self.worker_files.setHorizontalHeaderLabels(
                ["Remote path", "Size", "Downloaded", "Present"]
            )
            self.worker_files.setEditTriggers(
                QAbstractItemView.EditTrigger.NoEditTriggers
            )
            self.worker_files.setMaximumHeight(130)
            self.worker_files.verticalHeader().setVisible(False)
            self.worker_files.horizontalHeader().setSectionResizeMode(
                0, QHeaderView.ResizeMode.Stretch
            )
            storage_layout.addWidget(self.worker_files)
            layout.addWidget(storage_group)
            return tab

        def show_ssh_pair_dialog(self) -> None:
            if self._connection_state != "online" or self._ssh_in_flight:
                self.statusBar().showMessage(
                    "Wait for the controller to connect before adding a computer.", 7000
                )
                return
            from ateable.ssh_devices import SshDeviceStore

            store = SshDeviceStore(default_config_path().with_name("ssh-devices.json"))
            try:
                saved = store.load()
            except (OSError, RuntimeError):
                saved = {}
            dialog = QDialog(self)
            dialog.setWindowTitle("Connect a Linux download computer")
            dialog.setMinimumWidth(590)
            layout = QVBoxLayout(dialog)
            hint = QLabel(
                "Enter an SSH host, saved alias, or an address on your existing private network. Ateable installs its worker. Downloads stay there; keep Admin open for this SSH connection."
            )
            hint.setWordWrap(True)
            layout.addWidget(hint)
            form = QFormLayout()
            advanced_panel = QWidget()
            advanced_panel.setObjectName("sshAdvancedOptions")
            advanced_form = QFormLayout(advanced_panel)
            advanced_form.setContentsMargins(0, 5, 0, 0)
            preset = QComboBox()
            preset.setObjectName("sshPreset")
            preset.addItem("New connection", None)
            for name, spec in saved.items():
                preset.addItem(name, spec)
            form.addRow("Saved SSH connection", preset)
            fields = {}
            for name, label, default in (
                ("host", "SSH host / config alias", ""),
                ("user", "User (optional)", ""),
                ("identity_file", "SSH key file (optional)", ""),
                (
                    "remote_dir",
                    "Remote installation directory",
                    "~/.local/share/ateable-worker",
                ),
                ("output_dir", "Download folder on that computer", ""),
                ("s3_endpoint", "Worker S3 endpoint (optional)", ""),
                ("s3_profile", "Worker AWS profile (optional)", ""),
                ("proxy", "Proxy URL (proxy mode)", ""),
            ):
                entry = QLineEdit(default)
                entry.setObjectName(f"ssh_{name}")
                if name == "proxy":
                    entry.setEchoMode(QLineEdit.EchoMode.Password)
                elif name == "s3_endpoint":
                    entry.setPlaceholderText(
                        "https://objects.example or http://minio:9000"
                    )
                    entry.setToolTip(
                        "HTTP(S) endpoint without credentials or query tokens. Leave blank to use the VM's S3 configuration."
                    )
                elif name == "s3_profile":
                    entry.setPlaceholderText("Profile configured on the worker VM")
                    entry.setToolTip(
                        "Name from the VM's AWS configuration; access keys remain on the VM. Leave blank to use its default profile."
                    )
                elif name == "host":
                    entry.setPlaceholderText("download-vm or your SSH alias")
                    entry.setToolTip(
                        "Use an ordinary SSH host or an address on an already configured private network, such as Tailscale. Ateable uses your SSH keys and known hosts; it does not install or manage the private network."
                    )
                elif name == "output_dir":
                    entry.setPlaceholderText(
                        "Leave blank to use the worker's default folder"
                    )
                elif name in {"user", "identity_file"}:
                    entry.setPlaceholderText(
                        "Leave blank to use your SSH configuration"
                    )
                target_form = (
                    form
                    if name in {"host", "user", "identity_file", "output_dir"}
                    else advanced_form
                )
                target_form.addRow(label, entry)
                fields[name] = entry
            port = QSpinBox()
            port.setRange(0, 65535)
            port.setSpecialValueText("SSH config / default")
            port.setValue(0)
            advanced_form.addRow("SSH port", port)
            concurrency = QSpinBox()
            concurrency.setRange(1, 64)
            concurrency.setValue(1)
            advanced_form.addRow("Concurrent downloads", concurrency)
            network = QComboBox()
            for label, mode in (
                ("Direct", "direct"),
                ("Tor", "tor"),
                ("Proxy", "proxy"),
            ):
                network.addItem(label, mode)
            advanced_form.addRow("Download network", network)
            workspace = QComboBox()
            workspace.addItem("Choose later", None)
            for entry in self.workspaces_tab.workspaces:
                workspace.addItem(entry["name"], entry["id"])
            workspace.setCurrentIndex(
                max(0, workspace.findData(self.workspaces_tab.workspace_id))
            )
            form.addRow("Workspace (optional)", workspace)
            reinstall = QCheckBox("Install or update worker dependencies")
            reinstall.setChecked(True)
            advanced_form.addRow(reinstall)

            def load_preset() -> None:
                spec = preset.currentData()
                if spec is None:
                    return
                for name, entry in fields.items():
                    entry.setText(str(getattr(spec, name) or ""))
                port.setValue(spec.port or 0)
                concurrency.setValue(spec.concurrency)
                network.setCurrentIndex(max(0, network.findData(spec.network_mode)))
                workspace.setCurrentIndex(max(0, workspace.findData(spec.workspace_id)))
                reinstall.setChecked(False)

            preset.currentIndexChanged.connect(load_preset)
            layout.addLayout(form)
            advanced_toggle = QToolButton()
            advanced_toggle.setObjectName("sshAdvancedToggle")
            advanced_toggle.setText("Advanced options")
            advanced_toggle.setCheckable(True)
            advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
            advanced_toggle.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextBesideIcon
            )
            advanced_toggle.setToolTip(
                "Installation path, SSH port, network, concurrency, and S3 settings. Saved values stay in effect when collapsed."
            )

            def toggle_advanced(expanded: bool) -> None:
                advanced_panel.setVisible(expanded)
                advanced_toggle.setArrowType(
                    Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
                )
                dialog.adjustSize()

            advanced_toggle.toggled.connect(toggle_advanced)
            layout.addWidget(advanced_toggle)
            layout.addWidget(advanced_panel)
            advanced_panel.setVisible(False)
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok
                | QDialogButtonBox.StandardButton.Cancel
            )
            buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Connect worker")
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            try:
                from ateable.ssh_devices import SshDeviceSpec

                spec = SshDeviceSpec(
                    **{
                        name: entry.text().strip() or None
                        for name, entry in fields.items()
                    },
                    port=port.value() or None,
                    concurrency=concurrency.value(),
                    network_mode=str(network.currentData()),
                    workspace_id=workspace.currentData(),
                )
                self.connect_ssh_worker(spec, install=reinstall.isChecked())
            except (ValueError, OSError) as exc:
                QMessageBox.warning(self, APP_NAME, str(exc))

        def connect_ssh_worker(self, spec: Any, *, install: bool = True) -> None:
            if self._ssh_in_flight:
                return
            from ateable.ssh_devices import SshDeviceStore, SshProvisioner

            if self._ssh_provisioner is None:
                self._ssh_provisioner = SshProvisioner()
            provisioner = self._ssh_provisioner
            client = self.client
            self._ssh_in_flight = True
            self.ssh_pair_button.setEnabled(False)
            self.statusBar().showMessage(
                "Installing remote worker and connecting through SSH…"
            )

            def connect() -> Any:
                if spec.workspace_id:
                    client.request("GET", f"/api/workspaces/{spec.workspace_id}")
                if install:
                    provisioner.provision(spec)
                pair_key = pairing_key_from_payload(client.create_pairing_key())
                handle = provisioner.connect(spec, client.base_url, pair_key=pair_key)
                if spec.workspace_id:
                    client.request(
                        "POST",
                        f"/api/workspaces/{spec.workspace_id}/members",
                        {"worker_id": handle.worker_id},
                    )
                return handle

            def connected(handle: Any) -> None:
                suffix = ""
                try:
                    SshDeviceStore(
                        default_config_path().with_name("ssh-devices.json")
                    ).save(spec.host, spec)
                except (OSError, ValueError, RuntimeError):
                    suffix = " Settings were not saved."
                self.statusBar().showMessage(
                    f"Worker {handle.worker_id} connected through SSH.{suffix}", 10000
                )
                self.refresh_devices()
                if spec.workspace_id:
                    self.workspaces_tab.refresh()

            def finished() -> None:
                self._ssh_in_flight = False
                self._update_start_status()

            self._run_async(connect, connected, on_finished=finished)

        def refresh_worker_storage(self, *, force: bool = False) -> None:
            rows = self.devices_table.selectionModel().selectedRows()
            selected = (
                self.devices_table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
                if rows
                else None
            )
            worker_id = str(selected) if selected else None
            if not worker_id:
                self._storage_worker_id = None
                self.worker_files.setRowCount(0)
                self.worker_storage_summary.setText(
                    "Select a worker to see its remote storage."
                )
                return
            if not force and worker_id == self._storage_worker_id:
                return
            self._storage_worker_id = worker_id
            self._storage_revision += 1
            revision = self._storage_revision
            self.worker_storage_summary.setText("Loading remote storage…")
            self.worker_files.setRowCount(0)
            client = self.client

            def loaded(payload: Any) -> None:
                if (
                    worker_id != self._storage_worker_id
                    or revision != self._storage_revision
                ):
                    return
                inventory = payload.get("inventory") or {}
                self.worker_storage_summary.setText(
                    f"{inventory.get('output_dir') or 'Worker output directory'} · "
                    f"{format_bytes(inventory.get('disk_free_bytes'))} free of {format_bytes(inventory.get('disk_total_bytes'))} · "
                    f"{format_bytes(payload.get('total_downloaded_bytes'))} completed output · "
                    f"{payload.get('completed_files', 0)} completed files · "
                    f"{inventory.get('file_count', '—')} files currently in output "
                    f"({format_bytes(inventory.get('downloaded_bytes'))}) · "
                    f"Last report: {format_timestamp(inventory.get('sampled_at'))}"
                    + (
                        " · Partial output scan"
                        if inventory.get("scan_complete") is False
                        else ""
                    )
                )
                self.worker_files.setRowCount(0)
                for entry in (payload.get("files") or [])[:100]:
                    row = self.worker_files.rowCount()
                    self.worker_files.insertRow(row)
                    values = (
                        entry.get("path", ""),
                        format_bytes(entry.get("size_bytes")),
                        format_timestamp(entry.get("downloaded_at")),
                        "Yes"
                        if entry.get("exists") is True
                        else "No"
                        if entry.get("exists") is False
                        else "Unknown",
                    )
                    for column, value in enumerate(values):
                        self.worker_files.setItem(
                            row, column, QTableWidgetItem(str(value))
                        )

            def failed(message: str) -> None:
                if (
                    worker_id == self._storage_worker_id
                    and revision == self._storage_revision
                ):
                    self.worker_storage_summary.setText(
                        f"Storage unavailable: {message}"
                    )

            self._run_async(
                lambda: client.worker_storage(worker_id), loaded, on_error=failed
            )

        def _build_settings_tab(
            self, controller_url: str, controller_key: str
        ) -> QWidget:
            tab = QWidget()
            tab_layout = QVBoxLayout(tab)
            tab_layout.setContentsMargins(0, 0, 0, 0)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            tab_layout.addWidget(scroll)

            content = QWidget()
            content.setObjectName("settingsContent")
            scroll.setWidget(content)
            outer = QVBoxLayout(content)
            outer.setContentsMargins(18, 16, 18, 16)
            outer.setSpacing(12)
            self._settings_loaded_once = False
            self._settings_loading = False
            self._settings_dirty = False
            self._loading_settings = False
            self._dirty_sections: set[str] = set()
            self._settings_revisions = {
                "controller": 0,
                "downloads": 0,
                "network": 0,
            }
            self._loaded_download_settings: dict[str, Any] = {}
            self._restart_required = False

            settings_intro = QHBoxLayout()
            intro_text = QVBoxLayout()
            intro_text.setSpacing(1)
            intro_title = QLabel("Application settings")
            intro_title.setObjectName("sectionTitle")
            intro_body = QLabel(
                "Controller connection and download defaults for this Admin session."
            )
            intro_body.setProperty("muted", True)
            intro_text.addWidget(intro_title)
            intro_text.addWidget(intro_body)
            settings_intro.addLayout(intro_text)
            settings_intro.addStretch(1)
            self.settings_state_label = QLabel("Loading…")
            self.settings_state_label.setObjectName("settingsState")
            self.settings_state_label.setProperty("state", "loading")
            settings_intro.addWidget(self.settings_state_label)
            outer.addLayout(settings_intro)

            controller_group = QGroupBox("Controller connection")
            controller_layout = QGridLayout(controller_group)
            controller_layout.addWidget(QLabel("URL"), 0, 0)
            self.controller_url = QLineEdit(controller_url)
            controller_layout.addWidget(self.controller_url, 0, 1)
            self.apply_controller_button = QPushButton("&Apply")
            self.apply_controller_button.clicked.connect(self.apply_controller_url)
            controller_layout.addWidget(self.apply_controller_button, 0, 2)
            self.test_controller_button = QPushButton("&Test connection")
            self.test_controller_button.clicked.connect(self.test_controller)
            controller_layout.addWidget(self.test_controller_button, 0, 3)
            controller_layout.addWidget(QLabel("Controller key"), 1, 0)
            self.controller_key = QLineEdit(controller_key)
            self.controller_key.setEchoMode(QLineEdit.EchoMode.Password)
            self.controller_key.setPlaceholderText(
                "Required when controller authentication is enabled"
            )
            controller_layout.addWidget(self.controller_key, 1, 1)
            self.controller_key_reveal = QToolButton()
            self.controller_key_reveal.setText("Show")
            self.controller_key_reveal.setCheckable(True)
            self.controller_key_reveal.toggled.connect(self._toggle_controller_key)
            controller_layout.addWidget(self.controller_key_reveal, 1, 2)
            self.controller_test_status = QLabel(
                "Test checks reachability and authenticated controller access."
            )
            self.controller_test_status.setObjectName("controllerTestStatus")
            self.controller_test_status.setProperty("muted", True)
            controller_layout.addWidget(self.controller_test_status, 2, 0, 1, 4)
            outer.addWidget(controller_group)

            storage_group = QGroupBox("Queue defaults")
            storage_layout = QGridLayout(storage_group)
            storage_layout.addWidget(QLabel("Worker output default"), 0, 0)
            self.output_folder = QLineEdit(
                str(getattr(self.device_config, "output_dir", "") or "")
            )
            storage_layout.addWidget(self.output_folder, 0, 1)
            browse_button = QPushButton("&Browse…")
            browse_button.clicked.connect(self.choose_output_folder)
            storage_layout.addWidget(browse_button, 0, 2)
            storage_layout.addWidget(QLabel("Default format"), 1, 0)
            self.default_format = QComboBox()
            for output_format in SUPPORTED_FORMATS:
                self.default_format.addItem(
                    "Keep original file"
                    if output_format == "original"
                    else output_format.upper(),
                    output_format,
                )
            storage_layout.addWidget(
                self.default_format, 1, 1, alignment=Qt.AlignmentFlag.AlignLeft
            )
            storage_layout.addWidget(QLabel("Concurrent jobs"), 2, 0)
            self.concurrency = QSpinBox()
            self.concurrency.setRange(1, 32)
            self.concurrency.setValue(1)
            storage_layout.addWidget(
                self.concurrency, 2, 1, alignment=Qt.AlignmentFlag.AlignLeft
            )
            storage_layout.addWidget(QLabel("Worker offline after"), 3, 0)
            self.worker_timeout = QSpinBox()
            self.worker_timeout.setRange(5, 3600)
            self.worker_timeout.setSuffix(" seconds")
            self.worker_timeout.setValue(45)
            storage_layout.addWidget(
                self.worker_timeout, 3, 1, alignment=Qt.AlignmentFlag.AlignLeft
            )
            download_actions = QHBoxLayout()
            download_actions.addStretch(1)
            self.reload_settings_button = QPushButton("Revert")
            self.reload_settings_button.clicked.connect(
                lambda: self.refresh_settings(force=True)
            )
            download_actions.addWidget(self.reload_settings_button)
            self.save_downloads_button = QPushButton("Save download settings")
            self.save_downloads_button.setProperty("primary", True)
            self.save_downloads_button.clicked.connect(self.save_download_settings)
            download_actions.addWidget(self.save_downloads_button)
            storage_layout.addLayout(download_actions, 4, 0, 1, 3)
            outer.addWidget(storage_group)
            for row in (0, 2):
                for column in range(3):
                    item = storage_layout.itemAtPosition(row, column)
                    if item is not None and item.widget() is not None:
                        item.widget().hide()

            network_group = QGroupBox("Network & privacy")
            network_layout = QGridLayout(network_group)
            network_layout.addWidget(QLabel("Mode"), 0, 0)
            self.network_mode = QComboBox()
            self.network_mode.addItem("Direct", "direct")
            self.network_mode.addItem("Tor (local SOCKS)", "tor")
            self.network_mode.addItem("Custom proxy", "proxy")
            saved_mode = str(
                getattr(self.device_config, "network_mode", "")
                or self.local_settings.value("network_mode", "tor")
            )
            self.active_network_mode = saved_mode
            mode_index = self.network_mode.findData(saved_mode)
            if mode_index < 0:
                mode_index = self.network_mode.findData("tor")
            self.network_mode.setCurrentIndex(mode_index)
            self.active_network_mode = str(self.network_mode.currentData())
            network_layout.addWidget(self.network_mode, 0, 1)
            self.tor_proxy_label = QLabel("Tor SOCKS URL")
            network_layout.addWidget(self.tor_proxy_label, 1, 0)
            self.active_tor_proxy = str(
                getattr(
                    self.device_config,
                    "tor_proxy",
                    "socks5h://127.0.0.1:9050",
                )
            )
            self.tor_proxy_input = QLineEdit(self.active_tor_proxy)
            self.tor_proxy_input.setPlaceholderText("socks5h://127.0.0.1:9050")
            network_layout.addWidget(self.tor_proxy_input, 1, 1)
            self.custom_proxy_label = QLabel("Custom proxy URL")
            network_layout.addWidget(self.custom_proxy_label, 2, 0)
            configured_proxy = str(getattr(self.device_config, "proxy", "") or "")
            self.active_custom_proxy = configured_proxy
            if self.device_config is None:
                self.active_custom_proxy = str(
                    self.local_settings.value("custom_proxy", "")
                )
            self.custom_proxy = QLineEdit(self.active_custom_proxy)
            self.custom_proxy.setPlaceholderText(
                "https://, http://, socks5://, or socks5h://"
            )
            network_layout.addWidget(self.custom_proxy, 2, 1)
            self.save_network_button = QPushButton("Save network settings")
            self.save_network_button.clicked.connect(self.save_network_settings)
            network_layout.addWidget(self.save_network_button, 3, 1)
            self.probe_network_button = QPushButton("&Probe connection")
            self.probe_network_button.clicked.connect(self.probe_network)
            network_layout.addWidget(self.probe_network_button, 0, 2)
            self.network_help = QLabel()
            self.network_help.setWordWrap(True)
            self.network_help.setProperty("muted", True)
            network_layout.addWidget(self.network_help, 4, 0, 1, 3)
            self.network_status = QLabel("Not probed")
            self.network_status.setObjectName("networkStatus")
            self.network_status.setProperty("state", "idle")
            network_layout.addWidget(self.network_status, 5, 0, 1, 3)
            outer.addWidget(network_group)
            network_group.hide()
            worker_network_hint = QLabel(
                "SSH or your existing private network carries control messages. Download routes (Direct, Tor, or Proxy) and output folders are configured per worker when pairing."
            )
            worker_network_hint.setWordWrap(True)
            outer.addWidget(worker_network_hint)

            for field in (self.controller_url, self.controller_key):
                field.textEdited.connect(
                    lambda _text, section="controller": self._mark_settings_dirty(
                        section
                    )
                )
            self.output_folder.textEdited.connect(
                lambda _text: self._mark_settings_dirty("downloads")
            )
            for field in (self.default_format, self.concurrency, self.worker_timeout):
                if isinstance(field, QComboBox):
                    field.currentIndexChanged.connect(
                        lambda _value: self._mark_settings_dirty("downloads")
                    )
                else:
                    field.valueChanged.connect(
                        lambda _value: self._mark_settings_dirty("downloads")
                    )
            for field in (self.tor_proxy_input, self.custom_proxy):
                field.textEdited.connect(
                    lambda _text, section="network": self._mark_settings_dirty(section)
                )
            self.network_mode.currentIndexChanged.connect(self._network_mode_changed)
            self.network_mode.currentIndexChanged.connect(
                lambda _value: self._mark_settings_dirty("network")
            )

            outer.addStretch(1)
            self._network_mode_changed()
            return tab

        def _install_actions(self) -> None:
            refresh_action = QAction("Refresh", self)
            refresh_action.setShortcuts([QKeySequence.Refresh, QKeySequence("F5")])
            refresh_action.triggered.connect(self._poll_current_tab)
            self.addAction(refresh_action)
            self._shortcuts = []

            def shortcut(keys: str, operation: Callable[[], None]) -> None:
                item = QShortcut(QKeySequence(keys), self)
                item.activated.connect(operation)
                self._shortcuts.append(item)

            shortcut("Ctrl+Return", self.enqueue_urls)
            shortcut("Ctrl+K", self._focus_job_search)
            shortcut("Ctrl+N", self._focus_url_input)
            shortcut("Ctrl+O", self.import_url_list)
            for number, widget in enumerate(self._tab_names.values(), start=1):
                shortcut(
                    f"Ctrl+{number}",
                    lambda target=widget: self.tabs.setCurrentWidget(target),
                )

        def _apply_style(self) -> None:
            self.setStyleSheet(
                """
                QMainWindow, #appRoot { background: #000000; color: #e5e8ec; }
                QWidget { background: #000000; color: #e5e8ec; }
                QDialog, QMessageBox { background: #000000; color: #e5e8ec; }
                QDialog QLabel { background: transparent; }
                #appHeader {
                    background: #000000;
                    border-bottom: 1px solid #30343b;
                }
                #appTitle {
                    color: #f7f9fb;
                    font-size: 20px;
                    font-weight: 750;
                    letter-spacing: 2px;
                }
                #appSubtitle, #versionLabel { color: #89919c; }
                #startTitle { color: white; font-size: 26px; font-weight: 750; }
                #startIntro { color: #c8c8c8; font-size: 13px; }
                #startReadiness {
                    background: #000000; border: 1px solid #676767; border-radius: 9px;
                }
                #startStep {
                    background: #000000; border: 1px solid #383838; border-radius: 8px;
                }
                #startNumber { color: white; font-size: 26px; font-weight: 750; }
                #startStepTitle { color: #f4f4f4; font-size: 14px; font-weight: 700; }
                #versionLabel { padding: 5px 2px; }
                #connectionPill {
                    background: #000000;
                    color: #aab2bd;
                    border: 1px solid #3b4049;
                    border-radius: 13px;
                    padding: 5px 11px;
                    font-weight: 600;
                }
                #connectionPill[state="online"] {
                    background: #000000;
                    color: #8bd8bf;
                    border-color: #2c5a4e;
                }
                #connectionPill[state="error"] {
                    background: #000000;
                    color: #ef9da6;
                    border-color: #61383e;
                }
                QTabWidget::pane {
                    border: 0;
                    border-top: 1px solid #30343b;
                    background: #000000;
                }
                QTabBar { background: #000000; }
                QTabBar::tab {
                    background: #000000;
                    color: #929aa5;
                    padding: 11px 22px 10px 22px;
                    border: 0;
                    border-bottom: 2px solid transparent;
                    font-weight: 600;
                }
                QTabBar::tab:selected {
                    background: #000000;
                    color: #f3f5f7;
                    border-bottom-color: #f4f4f4;
                }
                QTabBar::tab:hover:!selected { color: #d7dce2; background: #202329; }
                QTabBar::tab:focus { outline: none; }
                QGroupBox {
                    background: #000000;
                    border: 1px solid #343941;
                    border-radius: 8px;
                    margin-top: 12px;
                    padding: 13px 11px 11px 11px;
                    font-weight: 600;
                }
                QGroupBox::title {
                    subcontrol-origin: margin;
                    left: 10px;
                    padding: 0 6px;
                    color: #dce1e7;
                }
                QLineEdit, QPlainTextEdit, QComboBox, QSpinBox {
                    background: #000000;
                    color: #eef1f4;
                    border: 1px solid #414751;
                    border-radius: 6px;
                    padding: 7px 8px;
                    selection-background-color: #4d6f96;
                }
                #bucketList, #spaceMembers, #spaceAgents {
                    background: #000000;
                    border: 1px solid #343941;
                    border-radius: 6px;
                }
                #bucketList::item, #spaceMembers::item, #spaceAgents::item { padding: 12px 10px; }
                #bucketList::item:selected, #spaceMembers::item:selected, #spaceAgents::item:selected { background: #304860; color: #f3f5f7; }
                #bucketList::item:hover:!selected, #spaceMembers::item:hover:!selected, #spaceAgents::item:hover:!selected { background: #282c32; }
                QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus {
                    border: 1px solid #79a9d3;
                }
                QLineEdit:disabled, QPlainTextEdit:disabled, QComboBox:disabled,
                QSpinBox:disabled { background: #000000; color: #737b85; }
                QPushButton, QToolButton {
                    background: #000000;
                    color: #e4e8ec;
                    border: 1px solid #464c56;
                    border-radius: 6px;
                    padding: 7px 11px;
                }
                QPushButton:hover, QToolButton:hover { background: #363b43; border-color: #59616c; }
                QPushButton:pressed, QToolButton:pressed { background: #23262c; }
                QPushButton:focus, QToolButton:focus { border-color: #79a9d3; }
                QPushButton[primary="true"] {
                    background: #f4f4f4;
                    border-color: #ffffff;
                    color: #111111;
                    font-weight: 700;
                    min-width: 88px;
                }
                QPushButton[primary="true"]:hover { background: #d6d6d6; }
                QPushButton[quiet="true"] { background: transparent; }
                QPushButton:disabled, QToolButton:disabled {
                    background: #000000;
                    border-color: #343840;
                    color: #68707a;
                }
                QLabel[muted="true"] { color: #8b939e; font-weight: 400; }
                #sectionTitle { font-size: 17px; font-weight: 700; color: #f0f2f5; }
                #settingsState, #networkStatus, #controllerTestStatus {
                    background: #000000;
                    color: #9ea6b0;
                    border: 1px solid #3b4048;
                    border-radius: 11px;
                    padding: 4px 9px;
                }
                #settingsState[state="dirty"] { color: #f0c47c; border-color: #695331; }
                #settingsState[state="saved"], #networkStatus[state="success"],
                #controllerTestStatus[state="success"] {
                    color: #88d3ba;
                    border-color: #31594e;
                    background: #000000;
                }
                #settingsState[state="error"], #networkStatus[state="error"],
                #controllerTestStatus[state="error"] {
                    color: #ee9ca5;
                    border-color: #623940;
                    background: #000000;
                }
                #statCard {
                    background: #000000;
                    border: 1px solid #343941;
                    border-radius: 8px;
                }
                #statCard[tone="active"] { border-left: 3px solid #75a9d6; }
                #statCard[tone="success"] { border-left: 3px solid #69bea3; }
                #statCard[tone="warning"] { border-left: 3px solid #d3a85e; }
                #statValue { font-size: 19px; font-weight: 750; color: #f3f5f7; }
                #statLabel { color: #8d96a1; font-size: 11px; }
                #emptyState {
                    background: #000000;
                    border: 1px dashed #3a4048;
                    border-radius: 8px;
                }
                #emptyTitle { color: #dfe4e9; font-size: 17px; font-weight: 700; }
                #emptyBody { color: #858e99; }
                QLabel[state="valid"] { color: #7bc9ad; }
                QLabel[state="warning"] { color: #dfb66f; }
                QTableWidget {
                    background: #000000;
                    alternate-background-color: #000000;
                    color: #dde2e7;
                    border: 1px solid #343941;
                    border-radius: 7px;
                    gridline-color: transparent;
                    selection-background-color: #34536f;
                    selection-color: white;
                }
                QAbstractItemView { background: #000000; selection-background-color: #34536f; selection-color: white; }
                QTableCornerButton::section { background: #000000; border: 0; }
                QMenu { background: #000000; border: 1px solid #414751; }
                QMenu::item:selected { background: #282c32; color: white; }
                QHeaderView::section {
                    background: #000000;
                    color: #aeb6c0;
                    border: 0;
                    border-right: 1px solid #333841;
                    border-bottom: 1px solid #363b43;
                    padding: 9px 8px;
                    font-weight: 700;
                }
                QProgressBar {
                    background: #000000;
                    border: 1px solid #353a42;
                    border-radius: 5px;
                    text-align: center;
                    min-width: 90px;
                    max-height: 18px;
                    color: #dfe4e9;
                }
                QProgressBar::chunk { background: #5c8bb4; border-radius: 4px; }
                QProgressBar[status="completed"]::chunk { background: #4d9f87; }
                QProgressBar[status="failed"]::chunk { background: #a4555f; }
                QStatusBar {
                    background: #000000;
                    color: #a0a8b2;
                    border-top: 1px solid #30343b;
                    padding: 2px 8px;
                }
                QScrollArea, QScrollArea > QWidget > QWidget, #settingsContent {
                    background: #000000;
                }
                QScrollBar:vertical {
                    background: #000000; width: 10px; margin: 0;
                }
                QScrollBar::handle:vertical {
                    background: #3b414a; border-radius: 5px; min-height: 28px;
                }
                QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
                QToolTip {
                    background: #000000; color: #edf0f3;
                    border: 1px solid #515863; padding: 5px;
                }
                """
            )

        def _run_async(
            self,
            operation: Callable[[], Any],
            on_success: Callable[[Any], None],
            *,
            on_error: Callable[[str], None] | None = None,
            on_finished: Callable[[], None] | None = None,
        ) -> None:
            connection_generation = self._connection_generation
            task = ApiTask(operation)
            self._tasks.add(task)

            def succeed(result: Any) -> None:
                if (
                    self._closing
                    or connection_generation != self._connection_generation
                ):
                    return
                try:
                    on_success(result)
                except Exception as exc:  # noqa: BLE001 - surface UI callback bugs
                    self._report_error(str(exc))

            def fail(message: str) -> None:
                if (
                    not self._closing
                    and connection_generation == self._connection_generation
                ):
                    (on_error or self._report_error)(message)

            task.signals.success.connect(succeed)
            task.signals.error.connect(fail)

            def finish() -> None:
                self._tasks.discard(task)
                if (
                    on_finished
                    and not self._closing
                    and connection_generation == self._connection_generation
                ):
                    on_finished()

            task.signals.finished.connect(finish)
            self.thread_pool.start(task)

        def _invalidate_connection_tasks(self) -> None:
            """Ignore callbacks from the old controller and unlock its UI state."""

            self._connection_generation += 1
            self._workers_verified = False
            self.workers = []
            self._set_connection_state("connecting")
            self.buckets_tab.reset()
            self.workspaces_tab.reset()
            if self._ssh_provisioner is not None:
                self._ssh_provisioner.stop_all()
                self._ssh_provisioner = None
            self._ssh_in_flight = False
            self.ssh_pair_button.setEnabled(False)
            self._storage_worker_id = None
            self._storage_revision += 1
            self.worker_files.setRowCount(0)
            self.worker_storage_summary.setText(
                "Select a worker to see its remote storage."
            )
            self._jobs_loading = False
            self.job_search_timer.stop()
            self._jobs_offset = 0
            self._jobs_revision += 1
            self._jobs_match_total = 0
            self._jobs_server_filtered = False
            self.jobs = []
            self._jobs_total = 0
            self._job_counts = job_status_counts(())
            self._update_job_stats(self._job_counts)
            self._apply_job_filters()
            self._devices_loading = False
            self._settings_loading = False
            self._enqueue_in_flight = False
            self._pairing_in_flight = False
            self._probe_in_flight = False
            self.refresh_jobs_button.setEnabled(True)
            self.refresh_devices_button.setEnabled(True)
            self.reload_settings_button.setEnabled(True)
            self.enqueue_button.setText("&Start downloads")
            self.regenerate_pairing_button.setEnabled(True)
            self.regenerate_pairing_button.setText("&Regenerate…")
            self.probe_network_button.setEnabled(True)
            self.test_controller_button.setEnabled(True)
            self.save_downloads_button.setEnabled(True)
            self.save_downloads_button.setText("Save download settings")
            self._update_url_count()
            self._update_selected_action_state()

        def _report_error(self, message: str) -> None:
            self._set_connection_state("error", message)
            self.statusBar().showMessage(message, 10000)

        def _tab_changed(self, index: int) -> None:
            widget = self.tabs.widget(index)
            if widget is self.start_tab:
                self.refresh_devices()
            elif widget is self.queue_tab:
                self.refresh_jobs()
            elif widget is self.buckets_tab:
                self.buckets_tab.refresh()
                self.refresh_devices()
            elif widget is self.workspaces_tab:
                self.workspaces_tab.refresh()
                self.refresh_devices()
            elif widget is self.devices_tab:
                self.refresh_devices()
            elif widget is self.settings_tab and not self._settings_loaded_once:
                self.refresh_settings()

        def _poll_current_tab(self) -> None:
            if self.controller_runtime is not None:
                runtime_error = (
                    self.controller_runtime.error or self.controller_runtime.tor_error
                )
                if runtime_error:
                    self.statusBar().showMessage(runtime_error, 10000)
            widget = self.tabs.currentWidget()
            if widget is self.start_tab:
                self.refresh_devices()
            elif widget is self.queue_tab:
                self.refresh_jobs()
            elif widget is self.buckets_tab:
                self.buckets_tab.refresh()
                self.refresh_devices()
            elif widget is self.workspaces_tab:
                self.workspaces_tab.refresh()
                self.refresh_devices()
            elif widget is self.devices_tab:
                self.refresh_devices()

        def refresh_all(self) -> None:
            self.refresh_jobs()
            self.buckets_tab.refresh()
            self.workspaces_tab.refresh()
            self.refresh_devices()
            # Force the initial settings load, but preserve any edits the user has
            # made when a later connection refresh updates the other tabs.
            self.refresh_settings(
                force=(
                    not self._settings_loaded_once
                    and "downloads" not in self._dirty_sections
                )
            )

        def _focus_url_input(self) -> None:
            self._show_batch_composer()

        def _show_batch_composer(self) -> None:
            self.tabs.setCurrentWidget(self.queue_tab)
            self._batch_user_opened = True
            self.batch_group.setVisible(True)
            self.hide_batch_button.setVisible(bool(self.jobs))
            self.url_input.setFocus()

        def _hide_batch_composer(self, *_args: Any, force: bool = False) -> None:
            if not self.jobs and not force:
                return
            self._batch_user_opened = False
            self.batch_group.setVisible(False)
            self.jobs_table.setFocus()

        def _focus_job_search(self) -> None:
            self.tabs.setCurrentWidget(self.queue_tab)
            self.job_search.setFocus()
            self.job_search.selectAll()

        def _update_url_count(self) -> None:
            parsed = parse_urls(self.url_input.toPlainText())
            parts = [f"{len(parsed.urls):,} valid URLs"]
            if parsed.duplicates:
                parts.append(f"{len(parsed.duplicates):,} duplicates")
            if parsed.invalid:
                parts.append(f"{len(parsed.invalid):,} invalid/over limit")
            self.url_count.setText(" · ".join(parts))
            state = "valid" if parsed.urls else "empty"
            if parsed.invalid:
                state = "warning"
            self.url_count.setProperty("state", state)
            self._repolish(self.url_count)
            self.enqueue_button.setEnabled(
                bool(parsed.urls) and not self._enqueue_in_flight
            )

        def import_url_list(self) -> None:
            filename, _filter = QFileDialog.getOpenFileName(
                self,
                "Import download links",
                str(Path.home()),
                "URL lists (*.txt *.list *.urls);;All files (*)",
            )
            if not filename:
                return
            try:
                text = read_url_list(Path(filename))
                if not parse_urls(text).urls:
                    raise ValueError(
                        "No supported HTTP(S) download links were found in this file."
                    )
                existing = self.url_input.toPlainText()
                combined = parse_urls(existing + "\n" + text, limit=5001)
                if len(combined.urls) > 5000:
                    raise ValueError(
                        "Importing this list would exceed 5,000 links. "
                        "Enqueue the current draft or split the file into smaller batches."
                    )
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, APP_NAME, str(exc))
                return
            self._focus_url_input()
            cursor = self.url_input.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.beginEditBlock()
            cursor.insertText(
                ("\n" if existing and not existing.endswith("\n") else "") + text
            )
            cursor.endEditBlock()
            self.url_input.setTextCursor(cursor)
            added = len(combined.urls) - len(parse_urls(existing).urls)
            self.statusBar().showMessage(
                f"Imported {added:,} new links. Review the draft, then choose Enqueue.",
                7000,
            )

        def _selected_target_id(self) -> str | None:
            index = self.target_combo.currentIndex()
            if index >= 0:
                data = self.target_combo.itemData(index)
                if data:
                    return str(data)
            text = self.target_combo.currentText().strip()
            if not text or text == AUTO_TARGET_LABEL:
                return None
            for worker in self.workers:
                if text in {str(worker.get("id", "")), str(worker.get("name", ""))}:
                    return str(worker.get("id"))
            return text

        def enqueue_urls(self) -> None:
            if self._enqueue_in_flight:
                return
            submitted_text = self.url_input.toPlainText()
            parsed = parse_urls(submitted_text)
            if not parsed.urls:
                QMessageBox.warning(self, APP_NAME, "No valid download URLs.")
                return
            target = self._selected_target_id()
            output_format = str(self.format_combo.currentData())
            eligible = [
                worker
                for worker in self.workers
                if worker.get("id") != "local"
                and worker.get("status") == "online"
                and output_format in worker.get("capabilities", [])
                and (target is None or worker.get("id") == target)
            ]
            if not eligible:
                QMessageBox.warning(
                    self,
                    APP_NAME,
                    "Connect an online download computer that supports this format. Files download on that computer. You can save links in Buckets until it is ready.",
                )
                self.tabs.setCurrentWidget(self.devices_tab)
                return
            if parsed.invalid:
                answer = QMessageBox.question(
                    self,
                    APP_NAME,
                    f"{len(parsed.invalid):,} entries are invalid or exceed the 5,000 URL limit. "
                    f"Enqueue the {len(parsed.urls):,} valid URLs?",
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
            # Workers own their networking policy; the controller never supplies a local proxy.
            proxy = None
            self._enqueue_in_flight = True
            self.enqueue_button.setEnabled(False)
            self.enqueue_button.setText("Starting downloads…")
            subdir = self.output_subdir.text().strip() or None
            client = self.client

            def submit() -> dict[str, Any]:
                return client.create_batch(
                    parsed.urls,
                    output_format=output_format,
                    target_worker_id=target,
                    output_subdir=subdir,
                    proxy=proxy,
                )

            def submitted(result: Any) -> None:
                self._set_connection_state("online")
                batch = result.get("batch", {}) if isinstance(result, dict) else {}
                total = batch.get("total", len(parsed.urls))
                duplicate_count = (
                    len(result.get("duplicates", [])) if isinstance(result, dict) else 0
                )
                suffix = (
                    f"; {duplicate_count:,} server duplicates skipped"
                    if duplicate_count
                    else ""
                )
                self.statusBar().showMessage(
                    f"Enqueued {int(total):,} jobs{suffix}.", 8000
                )
                if self.url_input.toPlainText() == submitted_text:
                    self.url_input.clear()
                    self._hide_batch_composer(force=True)
                else:
                    self.statusBar().showMessage(
                        f"Enqueued {int(total):,} jobs{suffix}. Your changed draft was kept.",
                        8000,
                    )
                self.refresh_jobs()

            self._run_async(
                submit,
                submitted,
                on_finished=self._enqueue_finished,
            )

        def _enqueue_finished(self) -> None:
            self._enqueue_in_flight = False
            self.enqueue_button.setText("&Start downloads")
            self._update_url_count()

        def refresh_jobs(self) -> None:
            if self._jobs_loading or self._closing:
                return
            self.job_search_timer.stop()
            self._jobs_loading = True
            self.refresh_jobs_button.setEnabled(False)
            self._update_queue_pager()
            if not self.jobs:
                self.queue_summary.setText("Loading queue…")

            client = self.client
            revision = self._jobs_revision
            offset = self._jobs_offset
            status = str(self.job_status_filter.currentData() or "all")
            query = self.job_search.text().strip()

            def snapshot() -> dict[str, Any]:
                try:
                    payload = client.list_jobs(
                        limit=self._jobs_page_size,
                        offset=offset,
                        status=None if status == "all" else status,
                        query=query or None,
                    )
                except ControllerError as exc:
                    if exc.status != 422 or status not in {"active", "attention"}:
                        raise
                    # Older controllers do not understand grouped status filters.
                    payload = client.list_jobs(
                        limit=self._jobs_page_size, offset=offset
                    )
                result = dict(payload) if isinstance(payload, dict) else {}
                try:
                    result["stats"] = client.stats()
                except ControllerError:
                    # Older compatible controllers may not expose /api/stats.
                    # The loaded-page fallback keeps the queue usable.
                    result["stats"] = None
                return result

            def loaded(payload: Any) -> None:
                if revision != self._jobs_revision:
                    return
                self._set_connection_state("online")
                items = payload.get("items", []) if isinstance(payload, dict) else []
                total = (
                    payload.get("total", len(items))
                    if isinstance(payload, dict)
                    else len(items)
                )
                total = max(0, int(total))
                if offset and offset >= total:
                    self._jobs_offset = (
                        max(0, (total - 1) // self._jobs_page_size)
                        * self._jobs_page_size
                    )
                    self._jobs_revision += 1
                    return
                self.jobs = [item for item in items if isinstance(item, dict)]
                self._jobs_total = int(total)
                self._jobs_match_total = int(total)
                self._jobs_server_filtered = bool(payload.get("filters_applied"))
                self._jobs_loaded_revision = revision
                stats = payload.get("stats") if isinstance(payload, dict) else None
                if isinstance(stats, dict):
                    page_counts = job_status_counts(self.jobs)
                    self._job_counts = {
                        "total": max(0, int(stats.get("total", total))),
                        **{
                            status: max(0, int(stats.get(status, page_counts[status])))
                            for status in JOB_STATUSES
                        },
                    }
                    self._jobs_total = self._job_counts["total"]
                else:
                    self._job_counts = job_status_counts(self.jobs)
                    self._job_counts["total"] = self._jobs_total
                self._update_job_stats(self._job_counts)
                self._apply_job_filters()
                self._update_start_status()

            self._run_async(
                snapshot,
                loaded,
                on_error=lambda message: (
                    self._jobs_failed(message)
                    if revision == self._jobs_revision
                    else None
                ),
                on_finished=lambda: self._jobs_refresh_finished(revision),
            )

        def _jobs_failed(self, message: str) -> None:
            if self._local_controller_starting():
                self._set_connection_state("connecting", "Waiting for private network")
                self.queue_summary.setText("Starting private controller…")
                self.statusBar().showMessage("Preparing the controller…")
                if not self.jobs:
                    self.jobs_empty_title.setText("Preparing private controller")
                    self.jobs_empty_body.setText(
                        "Ateable will load the queue when the controller is ready."
                    )
                    self.jobs_stack.setCurrentIndex(1)
                return
            self._report_error(message)
            self.queue_summary.setText("Queue unavailable · click Refresh to retry")
            if not self.jobs:
                self.jobs_empty_title.setText("Could not load the queue")
                self.jobs_empty_body.setText(
                    "The controller did not respond. Check Settings, then retry."
                )
                self.jobs_stack.setCurrentIndex(1)

        def _jobs_refresh_finished(self, revision: int) -> None:
            self._jobs_loading = False
            self.refresh_jobs_button.setEnabled(True)
            self._update_queue_pager()
            if revision != self._jobs_revision and not self.job_search_timer.isActive():
                self.refresh_jobs()

        def _queue_filters_changed(self, *_args: Any) -> None:
            self._jobs_offset = 0
            self._jobs_revision += 1
            self._apply_job_filters()
            self.queue_summary.setText("Searching queue…")
            self.previous_jobs_button.setEnabled(False)
            self.next_jobs_button.setEnabled(False)
            self.job_search_timer.start()

        def _change_queue_page(self, direction: int) -> None:
            if self._jobs_loading or self.job_search_timer.isActive():
                return
            offset = self._jobs_offset + direction * self._jobs_page_size
            if offset < 0 or offset >= self._jobs_match_total:
                return
            self._jobs_offset = offset
            self._jobs_revision += 1
            self.jobs_table.clearSelection()
            self.jobs_table.verticalScrollBar().setValue(0)
            self.refresh_jobs()

        def _update_queue_pager(self) -> None:
            pages = max(
                1,
                (self._jobs_match_total + self._jobs_page_size - 1)
                // self._jobs_page_size,
            )
            self.queue_page_label.setText(
                f"Page {self._jobs_offset // self._jobs_page_size + 1:,} of {pages:,}"
            )
            self.queue_pager.setVisible(pages > 1 or self._jobs_offset > 0)
            idle = not self._jobs_loading and not self.job_search_timer.isActive()
            self.previous_jobs_button.setEnabled(idle and self._jobs_offset > 0)
            self.next_jobs_button.setEnabled(
                idle
                and self._jobs_offset + self._jobs_page_size < self._jobs_match_total
            )

        def _update_job_stats(self, counts: dict[str, int] | None = None) -> None:
            counts = counts or job_status_counts(self.jobs)
            active = counts["queued"] + counts["downloading"]
            attention = counts["paused"] + counts["failed"]
            self.total_jobs_value.setText(f"{self._jobs_total:,}")
            self.active_jobs_value.setText(f"{active:,}")
            self.completed_jobs_value.setText(f"{counts['completed']:,}")
            self.attention_jobs_value.setText(f"{attention:,}")
            self.pause_active_action.setEnabled(active > 0)
            self.resume_paused_action.setEnabled(counts["paused"] > 0)
            self.clear_completed_action.setEnabled(counts["completed"] > 0)

        def _apply_job_filters(self, *_args: Any) -> None:
            status = str(self.job_status_filter.currentData() or "all")
            query = self.job_search.text()
            displayed = (
                self.jobs
                if self._jobs_server_filtered
                and self._jobs_loaded_revision == self._jobs_revision
                else filter_jobs(self.jobs, status=status, query=query)
            )
            filtered = status != "all" or bool(query.strip())
            has_jobs = bool(self.jobs) or self._jobs_total > 0 or filtered
            self.queue_stats.setVisible(has_jobs)
            self.queue_filter_bar.setVisible(has_jobs)
            self.queue_action_bar.setVisible(has_jobs)
            if has_jobs:
                self.hide_batch_button.setVisible(True)
                if (
                    not self._batch_user_opened
                    and not self.url_input.toPlainText().strip()
                ):
                    self.batch_group.setVisible(False)
            else:
                self.batch_group.setVisible(True)
                self.hide_batch_button.setVisible(False)
            self.local_settings.setValue("queue_status_filter", status)
            self._populate_jobs(displayed)
            if filtered and not self._jobs_server_filtered:
                self.queue_summary.setText(
                    f"{len(displayed):,} page matches · older controller"
                )
                self.queue_summary.setToolTip(
                    "This controller can filter only the loaded page. "
                    "Update it to search the whole queue."
                )
            else:
                self.queue_summary.setToolTip("")
                start = self._jobs_offset + 1 if displayed else 0
                end = self._jobs_offset + len(displayed) if displayed else 0
                total = self._jobs_match_total if filtered else self._jobs_total
                noun = "matches" if filtered else "jobs"
                self.queue_summary.setText(f"{start:,}–{end:,} of {total:,} {noun}")
            self._update_queue_pager()
            if displayed:
                self.jobs_stack.setCurrentIndex(0)
            else:
                self.jobs_empty_title.setText(
                    "No matching jobs" if filtered else "No downloads yet"
                )
                self.jobs_empty_body.setText(
                    "Try a different search or status filter."
                    if filtered
                    else "Paste one or more download links above to create your first batch."
                )
                self.jobs_stack.setCurrentIndex(1)

        def _selected_job_ids(self) -> list[str]:
            selection = self.jobs_table.selectionModel()
            if selection is None:
                return []
            result: list[str] = []
            for index in selection.selectedRows(0):
                item = self.jobs_table.item(index.row(), 0)
                job_id = item.data(Qt.ItemDataRole.UserRole) if item else None
                if job_id:
                    result.append(str(job_id))
            return result

        def _populate_jobs(self, jobs: Iterable[dict[str, Any]] | None = None) -> None:
            displayed = list(self.jobs if jobs is None else jobs)
            selected_ids = set(self._selected_job_ids())
            scroll_position = self.jobs_table.verticalScrollBar().value()
            self.jobs_table.setRowCount(0)
            worker_names = {
                str(worker.get("id")): str(worker.get("name") or worker.get("id"))
                for worker in self.workers
                if worker.get("id")
            }
            status_colors = {
                "queued": QColor("#91a8bd"),
                "downloading": QColor("#78b8e8"),
                "paused": QColor("#e0bd79"),
                "completed": QColor("#79c9ac"),
                "failed": QColor("#ee929c"),
                "cancelled": QColor("#999fa8"),
            }
            for job in displayed:
                row = self.jobs_table.rowCount()
                self.jobs_table.insertRow(row)
                job_id = str(job.get("id", ""))
                title = str(job.get("title") or job.get("source_url") or "Untitled")
                source_item = QTableWidgetItem(title)
                source_item.setData(Qt.ItemDataRole.UserRole, job_id)
                tooltip_parts = [title, str(job.get("source_url") or "")]
                if job_id:
                    tooltip_parts.append(f"Job: {job_id}")
                if job.get("error"):
                    tooltip_parts.append(f"Error: {job['error']}")
                source_item.setToolTip(
                    "\n".join(part for part in tooltip_parts if part)
                )
                self.jobs_table.setItem(row, 0, source_item)
                self.jobs_table.setItem(
                    row, 1, QTableWidgetItem(str(job.get("format", "wav")).upper())
                )
                target_id = (
                    job.get("target_worker_id") or job.get("claimed_by") or "Controller"
                )
                target = worker_names.get(str(target_id), str(target_id))
                target_item = QTableWidgetItem(target)
                if str(target_id) != target:
                    target_item.setToolTip(str(target_id))
                self.jobs_table.setItem(row, 2, target_item)
                status = str(job.get("status", "unknown"))
                status_item = QTableWidgetItem(status.replace("_", " ").title())
                status_item.setToolTip(str(job.get("stage") or status))
                if status in status_colors:
                    status_item.setForeground(status_colors[status])
                self.jobs_table.setItem(row, 3, status_item)
                progress = QProgressBar()
                progress.setRange(0, 100)
                progress.setValue(progress_percent(job.get("progress"), status))
                progress.setProperty("status", status)
                progress.setFormat(
                    {
                        "queued": "Waiting",
                        "paused": "Paused",
                        "completed": "Done",
                        "failed": "Failed",
                        "cancelled": "Cancelled",
                    }.get(status, "%p%")
                )
                self.jobs_table.setCellWidget(row, 4, progress)
                updated = QTableWidgetItem(format_timestamp(job.get("updated_at")))
                updated.setToolTip(str(job.get("updated_at") or ""))
                self.jobs_table.setItem(row, 5, updated)
                self.jobs_table.setCellWidget(
                    row, 6, self._job_actions(job_id, status, title)
                )
                if job_id in selected_ids:
                    selection_model = self.jobs_table.selectionModel()
                    if selection_model is not None:
                        selection_model.select(
                            self.jobs_table.model().index(row, 0),
                            QItemSelectionModel.SelectionFlag.Select
                            | QItemSelectionModel.SelectionFlag.Rows,
                        )
            self.jobs_table.verticalScrollBar().setValue(scroll_position)
            self._update_selected_action_state()

        def _job_actions(self, job_id: str, status: str, title: str = "job") -> QWidget:
            holder = QWidget()
            layout = QHBoxLayout(holder)
            layout.setContentsMargins(2, 1, 2, 1)
            layout.setSpacing(3)
            actions: list[tuple[str, str]] = []
            if status in ACTIVE_JOB_STATUSES:
                actions.extend([("Pause", "pause"), ("Cancel", "cancel")])
            elif status == "paused":
                actions.extend([("Resume", "resume"), ("Cancel", "cancel")])
            elif status in {"failed", "cancelled"}:
                actions.append(("Retry", "retry"))
            else:
                label = QLabel("—")
                label.setProperty("muted", True)
                layout.addWidget(label)
            for label_text, action in actions:
                button = QToolButton()
                button.setText(label_text)
                button.setAccessibleName(f"{label_text} {title}")
                button.setToolTip(f"{label_text} this job")
                button.clicked.connect(
                    lambda _checked=False, jid=job_id, name=action: self.run_job_action(
                        jid, name
                    )
                )
                layout.addWidget(button)
            return holder

        def _update_selected_action_state(self, *_args: Any) -> None:
            count = len(self._selected_job_ids())
            action = self.selected_action.currentData()
            self.apply_selected_button.setEnabled(bool(count and action))
            self.apply_selected_button.setText(
                f"Apply to {count}" if count else "Apply"
            )

        def apply_selected_action(self) -> None:
            job_ids = self._selected_job_ids()
            action = self.selected_action.currentData()
            if not job_ids or action not in {"pause", "resume", "cancel", "retry"}:
                return
            if action == "cancel":
                answer = QMessageBox.question(
                    self,
                    APP_NAME,
                    f"Cancel {len(job_ids):,} selected job(s)?",
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
            self.apply_selected_button.setEnabled(False)
            client = self.client
            self._run_async(
                lambda: client.bulk_action(str(action), job_ids=job_ids),
                lambda result: self._selected_action_completed(str(action), result),
                on_finished=self._update_selected_action_state,
            )

        def _selected_action_completed(self, action: str, result: Any) -> None:
            updated = result.get("updated", 0) if isinstance(result, dict) else 0
            errors = result.get("errors", {}) if isinstance(result, dict) else {}
            suffix = f" · {len(errors):,} skipped" if errors else ""
            self.statusBar().showMessage(
                f"{action.title()} applied to {int(updated):,} job(s){suffix}.",
                7000,
            )
            self.selected_action.setCurrentIndex(0)
            self.refresh_jobs()

        def run_job_action(self, job_id: str, action: str) -> None:
            client = self.client
            self._run_async(
                lambda: client.job_action(job_id, action),
                lambda _result: self.refresh_jobs(),
            )

        def pause_active(self) -> None:
            client = self.client

            def operation() -> dict[str, Any]:
                updated = 0
                errors: list[str] = []
                for status in ("queued", "downloading"):
                    try:
                        result = client.bulk_action("pause", status=status)
                        updated += int(result.get("updated", 0))
                    except ControllerError as exc:
                        errors.append(f"{status}: {exc}")
                if errors and not updated:
                    raise ControllerError("; ".join(errors))
                return {"updated": updated, "errors": errors}

            self._run_async(
                operation,
                lambda result: self._bulk_action_completed("Paused", result),
            )

        def _bulk_status_action(self, action: str, status: str) -> None:
            client = self.client
            self._run_async(
                lambda: client.bulk_action(action, status=status),
                lambda result: self._bulk_action_completed(action.title(), result),
            )

        def _bulk_action_completed(self, verb: str, result: Any) -> None:
            updated = result.get("updated", 0) if isinstance(result, dict) else 0
            errors = result.get("errors", []) if isinstance(result, dict) else []
            suffix = " · some statuses could not be updated" if errors else ""
            self.statusBar().showMessage(
                f"{verb} {int(updated):,} job(s){suffix}.", 7000
            )
            self.refresh_jobs()

        def clear_completed(self) -> None:
            answer = QMessageBox.question(
                self,
                APP_NAME,
                "Remove all completed jobs from the queue history?\n\nDownloaded files are not deleted.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._run_async(
                self.client.clear_completed,
                lambda result: self._completed_cleared(result),
            )

        def _completed_cleared(self, result: Any) -> None:
            deleted = result.get("deleted", 0) if isinstance(result, dict) else 0
            self.statusBar().showMessage(
                f"Cleared {int(deleted):,} completed jobs.", 6000
            )
            self.refresh_jobs()

        def refresh_devices(self) -> None:
            if self._devices_loading:
                return
            self._devices_loading = True
            self.refresh_devices_button.setEnabled(False)
            if not self.workers:
                self.devices_summary.setText("Loading download computers…")

            client = self.client

            def fetch() -> dict[str, Any]:
                snapshot: dict[str, Any] = {"errors": []}
                try:
                    snapshot["workers"] = client.list_workers()
                except ControllerError as exc:
                    snapshot["errors"].append(f"workers: {exc}")
                return snapshot

            self._run_async(
                fetch,
                self._populate_devices,
                on_finished=self._devices_refresh_finished,
            )

        def _devices_refresh_finished(self) -> None:
            self._devices_loading = False
            self.refresh_devices_button.setEnabled(True)

        def _populate_devices(self, snapshot: Any) -> None:
            if not isinstance(snapshot, dict):
                return
            workers_payload = snapshot.get("workers")
            if isinstance(workers_payload, dict):
                items = workers_payload.get("items", [])
                self.workers = [item for item in items if isinstance(item, dict)]
            errors = snapshot.get("errors", [])
            if errors:
                self._workers_verified = False
                if self._local_controller_starting():
                    self._set_connection_state(
                        "connecting", "Waiting for private network"
                    )
                    self.devices_summary.setText("Starting private controller…")
                    if not self.workers:
                        self.devices_empty_title.setText("Preparing private controller")
                        self.devices_empty_body.setText(
                            "Download workers will appear when the controller is ready."
                        )
                        self.devices_stack.setCurrentIndex(1)
                    return
                message = "; ".join(str(error) for error in errors)
                self._report_error(message)
                self.devices_summary.setText(
                    "Could not refresh workers · showing the last snapshot"
                    if self.workers
                    else "Download workers unavailable"
                )
                if not self.workers:
                    self.devices_empty_title.setText("Could not load workers")
                    self.devices_empty_body.setText(
                        "The controller did not respond. Check Settings, then retry."
                    )
                    self.devices_stack.setCurrentIndex(1)
                return

            self._workers_verified = isinstance(workers_payload, dict)
            self._set_connection_state("online")
            self.devices_summary.setText(f"Download computers ({len(self.workers):,})")
            selected_worker_id = self._storage_worker_id
            self.devices_table.blockSignals(True)
            self.devices_table.setRowCount(0)
            online = 0
            capacity = 0
            status_colors = {
                "online": QColor("#79c9ac"),
                "offline": QColor("#d6ac65"),
            }
            for worker in self.workers:
                row = self.devices_table.rowCount()
                self.devices_table.insertRow(row)
                worker_status = str(worker.get("status") or "unknown")
                if worker_status == "online":
                    online += 1
                try:
                    capacity += int(worker.get("max_concurrency") or 0)
                except (TypeError, ValueError):
                    pass
                values = (
                    worker.get("name") or "Unnamed",
                    worker.get("id") or "—",
                    worker_status.title(),
                    worker.get("max_concurrency") or "—",
                    format_timestamp(worker.get("last_seen_at")),
                )
                for column, value in enumerate(values):
                    item = QTableWidgetItem(str(value))
                    if column == 2 and worker_status in status_colors:
                        item.setForeground(status_colors[worker_status])
                    if column == 0:
                        item.setData(Qt.ItemDataRole.UserRole, worker.get("id"))
                        capabilities = ", ".join(
                            str(entry).upper()
                            for entry in worker.get("capabilities", [])
                        )
                        output_dir = str(worker.get("output_dir") or "")
                        item.setToolTip(
                            "\n".join(
                                part
                                for part in (
                                    f"Formats: {capabilities}" if capabilities else "",
                                    f"Output: {output_dir}" if output_dir else "",
                                )
                                if part
                            )
                        )
                    self.devices_table.setItem(row, column, item)
                if worker.get("id") == selected_worker_id:
                    self.devices_table.selectRow(row)
            self.devices_table.blockSignals(False)
            if not self.devices_table.selectionModel().selectedRows():
                self._storage_worker_id = None
                self.worker_files.setRowCount(0)
                self.worker_storage_summary.setText(
                    "Select a worker to see its remote storage."
                )
            self.online_workers_value.setText(f"{online:,}")
            self.offline_workers_value.setText(f"{len(self.workers) - online:,}")
            self.worker_capacity_value.setText(f"{capacity:,}")
            self.devices_stack.setCurrentIndex(0 if self.workers else 1)
            if not self.workers:
                self.devices_empty_title.setText("Connect your first download computer")
                self.devices_empty_body.setText(
                    "Use the SSH button above. Ateable will install and connect its worker."
                )
            self._update_target_devices()
            self.buckets_tab.update_workers()
            self.workspaces_tab.update_workers()

        def _update_target_devices(self) -> None:
            previous_data = self.target_combo.currentData()
            previous_text = self.target_combo.currentText()
            self.target_combo.blockSignals(True)
            self.target_combo.clear()
            self.target_combo.addItem(AUTO_TARGET_LABEL, None)
            for worker in self.workers:
                worker_id = str(worker.get("id", ""))
                if not worker_id or worker_id == "local":
                    continue
                name = str(worker.get("name") or worker_id)
                status = str(worker.get("status", "unknown"))
                self.target_combo.addItem(f"{name} · {status}", worker_id)
            if previous_data:
                matched = self.target_combo.findData(previous_data)
                if matched >= 0:
                    self.target_combo.setCurrentIndex(matched)
                else:
                    self.target_combo.setEditText(str(previous_data))
            elif previous_text and previous_text != AUTO_TARGET_LABEL:
                self.target_combo.setEditText(previous_text)
            else:
                self.target_combo.setCurrentIndex(0)
            self.target_combo.blockSignals(False)

        def copy_pairing_key(self) -> None:
            key = self.pairing_key.text().strip()
            if key:
                QApplication.clipboard().setText(key)
                self.statusBar().showMessage("Pairing key copied.", 3000)
            else:
                self.statusBar().showMessage("No pairing key is available.", 3000)

        def copy_device_id(self) -> None:
            device_id = self.device_id_label.text().strip()
            if device_id:
                QApplication.clipboard().setText(device_id)
                self.statusBar().showMessage("Device ID copied.", 3000)

        def _toggle_pairing_key(self, visible: bool) -> None:
            self.pairing_key.setEchoMode(
                QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
            )
            self.pairing_reveal.setText("Hide" if visible else "Show")

        def regenerate_pairing_key(self) -> None:
            if self._pairing_in_flight:
                return
            if self.pairing_key.text().strip():
                answer = QMessageBox.question(
                    self,
                    APP_NAME,
                    "Regenerate the pairing key?\n\nThe current unused key will stop working.",
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
            self._pairing_in_flight = True
            self.regenerate_pairing_button.setEnabled(False)
            self.regenerate_pairing_button.setText("Generating…")
            self._run_async(
                self.client.regenerate_pairing_key,
                self._pairing_key_regenerated,
                on_finished=self._pairing_regeneration_finished,
            )

        def _pairing_key_regenerated(self, payload: Any) -> None:
            key = pairing_key_from_payload(payload)
            if not key:
                self._report_error("Controller did not return a pairing key.")
                return
            self.pairing_key.setText(key)
            self.pairing_reveal.setChecked(False)
            self.statusBar().showMessage(
                "New single-use pairing key generated. It expires in 10 minutes.",
                7000,
            )
            if key and self.device_config is not None:
                try:
                    self.device_config = self.device_config.updated(last_pair_key=key)
                    self.device_config.save()
                except (OSError, ValueError) as exc:
                    self._report_error(str(exc))

        def _pairing_regeneration_finished(self) -> None:
            self._pairing_in_flight = False
            self.regenerate_pairing_button.setEnabled(True)
            self.regenerate_pairing_button.setText("&Regenerate…")

        def _set_settings_state(self, text: str, state: str) -> None:
            self.settings_state_label.setText(text)
            self.settings_state_label.setProperty("state", state)
            self._repolish(self.settings_state_label)

        def _sync_settings_state(self, saved_text: str = "Up to date") -> None:
            self._settings_dirty = bool(self._dirty_sections)
            if self._settings_dirty:
                self._set_settings_state("Unsaved changes", "dirty")
            elif self._restart_required:
                self._set_settings_state("Saved · restart required", "dirty")
            else:
                self._set_settings_state(saved_text, "saved")

        def _mark_settings_dirty(self, section: str = "downloads", *_args: Any) -> None:
            if self._loading_settings:
                return
            self._dirty_sections.add(section)
            self._settings_revisions[section] = (
                self._settings_revisions.get(section, 0) + 1
            )
            self._sync_settings_state()

        def refresh_settings(self, *, force: bool = False) -> None:
            if self._settings_loading:
                return
            if "downloads" in self._dirty_sections and not force:
                return
            self._settings_loading = True
            self.reload_settings_button.setEnabled(False)
            self._set_settings_state("Loading…", "loading")
            request_revision = self._settings_revisions["downloads"]
            client = self.client
            self._run_async(
                client.get_settings,
                lambda values: self._settings_loaded(
                    values,
                    expected_revision=request_revision,
                ),
                on_error=self._settings_failed,
                on_finished=self._settings_refresh_finished,
            )

        def _settings_failed(self, message: str) -> None:
            if self._local_controller_starting():
                self._set_connection_state("connecting", "Waiting for private network")
                self._set_settings_state("Controller starting", "loading")
                return
            self._report_error(message)
            self._set_settings_state("Load failed", "error")

        def _settings_refresh_finished(self) -> None:
            self._settings_loading = False
            self.reload_settings_button.setEnabled(True)

        def _settings_loaded(
            self,
            values: Any,
            *,
            expected_revision: int | None = None,
        ) -> bool:
            if not isinstance(values, dict):
                return False
            if (
                expected_revision is not None
                and expected_revision != self._settings_revisions["downloads"]
            ):
                return False
            self._loading_settings = True
            try:
                output_dir = values.get("output_dir")
                if isinstance(output_dir, str):
                    self.output_folder.setText(output_dir)
                default_format = values.get("default_format")
                if isinstance(default_format, str):
                    index = self.default_format.findData(default_format)
                    if index >= 0:
                        self.default_format.setCurrentIndex(index)
                        self.format_combo.setCurrentIndex(
                            max(0, self.format_combo.findData(default_format))
                        )
                concurrency = values.get("max_concurrent_downloads")
                if isinstance(concurrency, int):
                    self.concurrency.setValue(concurrency)
                timeout = values.get("worker_timeout_seconds")
                if isinstance(timeout, int):
                    self.worker_timeout.setValue(timeout)
            finally:
                self._loading_settings = False
            self._settings_loaded_once = True
            self._loaded_download_settings = dict(values)
            self._dirty_sections.discard("downloads")
            self._sync_settings_state()
            self._set_connection_state("online")
            return True

        def _toggle_controller_key(self, visible: bool) -> None:
            self.controller_key.setEchoMode(
                QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
            )
            self.controller_key_reveal.setText("Hide" if visible else "Show")

        def apply_controller_url(self) -> None:
            try:
                normalized = normalize_base_url(self.controller_url.text())
            except ValueError as exc:
                QMessageBox.warning(self, APP_NAME, str(exc))
                return
            previous_url = self.client.base_url
            previous_key = self.client.controller_key
            previous_device = self.device_config
            previous_runtime = self.controller_runtime
            self.controller_url.setText(normalized)
            key = self.controller_key.text().strip()
            next_device = self.device_config
            if BACKEND_AVAILABLE and DeviceConfig is not None:
                try:
                    device = self.device_config or DeviceConfig.defaults()
                    next_device = device.updated(controller_url=normalized)
                    if should_manage_local_controller(next_device) and not key:
                        self.controller_key.setText(previous_key)
                        QMessageBox.warning(
                            self,
                            APP_NAME,
                            "A local controller key cannot be empty. Generate or enter a key before applying this connection.",
                        )
                        return
                    next_device = next_device.updated(admin_key=key or None)
                    next_device.save()
                    self.local_settings.remove("controller_key")
                except (OSError, ValueError) as exc:
                    self._report_error(str(exc))
                    return

            restart_owned_runtime = bool(
                previous_runtime is not None
                and (normalized != previous_url or key != previous_key)
            )
            runtime_restarted = False
            if restart_owned_runtime and previous_runtime is not None:
                if not previous_runtime.stop():
                    if previous_device is not None:
                        try:
                            previous_device.save()
                        except OSError:
                            pass
                    self.controller_url.setText(previous_url)
                    self.controller_key.setText(previous_key)
                    self._report_error(
                        "The current controller is still shutting down; no replacement was started. Try Apply again shortly."
                    )
                    return
                self.controller_runtime = None

            self.device_config = next_device
            if (
                self.controller_runtime is None
                and should_manage_local_controller(self.device_config)
                and not controller_is_listening(normalized)
            ):
                try:
                    self.controller_runtime = InProcessController(
                        controller_app_config(self.device_config)
                    )
                    self.controller_runtime.start()
                    runtime_restarted = True
                except (OSError, RuntimeError, ValueError) as exc:
                    self._report_error(str(exc))
                    return
            elif restart_owned_runtime:
                runtime_restarted = True

            deferred_external_key = bool(
                previous_runtime is None
                and should_manage_local_controller(self.device_config)
                and normalized == previous_url
                and key != previous_key
                and controller_is_listening(normalized)
            )
            active_key = previous_key if deferred_external_key else key
            connection_changed = (
                normalized != previous_url or active_key != previous_key
            )
            if connection_changed:
                previous_client = self.client
                self._invalidate_connection_tasks()
                self.client = ControllerClient(
                    normalized,
                    controller_key=active_key,
                    timeout=previous_client.timeout,
                    opener=previous_client._opener,
                )
                self._settings_loaded_once = False
                if "downloads" not in self._dirty_sections:
                    self._loaded_download_settings = {}

            self.local_settings.setValue("controller_url", normalized)
            if BACKEND_AVAILABLE and DeviceConfig is not None:
                self.device_id_label.setText(self.device_config.device_id)
                self.device_role_label.setText(self.device_config.role)
                self.device_name_label.setText(self.device_config.name)
                self.local_settings.remove("controller_key")
            else:
                # Remote-client-only installations have no restricted DeviceConfig.
                self.local_settings.setValue("controller_key", key)

            self._dirty_sections.discard("controller")
            if runtime_restarted:
                self._restart_required = False
                if self.device_config is not None:
                    self.active_network_mode = str(self.device_config.network_mode)
                    self.active_tor_proxy = str(self.device_config.tor_proxy)
                    self.active_custom_proxy = str(self.device_config.proxy or "")
            elif deferred_external_key:
                self._restart_required = True
            self._sync_settings_state("Connection applied")
            if deferred_external_key:
                self.statusBar().showMessage(
                    "Controller key saved. Restart the controller to activate it; this session keeps the current key.",
                    10000,
                )
            else:
                self._set_connection_state("connecting")
                self.statusBar().showMessage("Controller connection updated.", 4000)
                self.refresh_all()

        def test_controller(self) -> None:
            try:
                normalized = normalize_base_url(self.controller_url.text())
            except ValueError as exc:
                QMessageBox.warning(self, APP_NAME, str(exc))
                return
            key = self.controller_key.text().strip()
            self.test_controller_button.setEnabled(False)
            self.controller_test_status.setText("Testing controller and credentials…")

            def test() -> dict[str, Any]:
                client = ControllerClient(normalized, controller_key=key)
                health = client.health()
                client.get_settings()
                return health

            def succeeded(payload: Any) -> None:
                status = (
                    payload.get("status", "healthy")
                    if isinstance(payload, dict)
                    else "healthy"
                )
                self.controller_test_status.setText(
                    f"Connected · credentials accepted · {status}"
                )
                self.controller_test_status.setProperty("state", "success")
                self._repolish(self.controller_test_status)
                active = (
                    normalized == self.client.base_url
                    and key == self.client.controller_key
                )
                if active:
                    self._set_connection_state("online")
                    self.refresh_devices()
                self.statusBar().showMessage(
                    "Controller connection and credentials are valid."
                    if active
                    else "Connection test passed. Apply these settings to switch controllers.",
                    6000,
                )

            def failed(message: str) -> None:
                self.controller_test_status.setText(
                    f"Connection test failed: {message}"
                )
                self.controller_test_status.setProperty("state", "error")
                self._repolish(self.controller_test_status)
                if (
                    normalized == self.client.base_url
                    and key == self.client.controller_key
                ):
                    self._report_error(message)
                else:
                    self.statusBar().showMessage(
                        "The unsaved connection failed its test. Your active connection was kept.",
                        7000,
                    )

            self._run_async(
                test,
                succeeded,
                on_error=failed,
                on_finished=lambda: self.test_controller_button.setEnabled(True),
            )

        def choose_output_folder(self) -> None:
            try:
                controller_url = normalize_base_url(self.controller_url.text())
            except ValueError:
                controller_url = self.client.base_url
            if not is_loopback_controller(controller_url):
                QMessageBox.information(
                    self,
                    APP_NAME,
                    "This is a remote controller. Enter a path that exists on the controller machine; a local folder picker would select the wrong computer.",
                )
                return
            selected = QFileDialog.getExistingDirectory(
                self,
                "Select output folder",
                self.output_folder.text() or os.path.expanduser("~"),
            )
            if selected:
                self.output_folder.setText(selected)

        def save_download_settings(self) -> None:
            values: dict[str, Any] = {
                "default_format": str(self.default_format.currentData()),
                "worker_timeout_seconds": self.worker_timeout.value(),
            }
            requires_controller_restart = False
            request_revision = self._settings_revisions["downloads"]
            client = self.client
            self.save_downloads_button.setEnabled(False)
            self.save_downloads_button.setText("Saving…")
            self._run_async(
                lambda: client.update_settings(values),
                lambda result: self._settings_saved(
                    result,
                    expected_revision=request_revision,
                    requires_controller_restart=requires_controller_restart,
                ),
                on_finished=self._download_settings_finished,
            )

        def _download_settings_finished(self) -> None:
            self.save_downloads_button.setEnabled(True)
            self.save_downloads_button.setText("Save download settings")

        def _settings_saved(
            self,
            values: Any,
            *,
            expected_revision: int | None = None,
            requires_controller_restart: bool = False,
        ) -> None:
            applied_to_form = self._settings_loaded(
                values,
                expected_revision=expected_revision,
            )
            if isinstance(values, dict) and not applied_to_form:
                self._loaded_download_settings = dict(values)
            output_dir = values.get("output_dir") if isinstance(values, dict) else None
            if output_dir and self.device_config is not None:
                try:
                    self.device_config = self.device_config.updated(
                        output_dir=str(output_dir)
                    )
                    self.device_config.save()
                except (OSError, ValueError) as exc:
                    self._report_error(str(exc))
            message = "Download settings saved."
            if requires_controller_restart:
                if should_manage_local_controller(self.device_config):
                    message += " Restart the app to apply the new concurrency."
                else:
                    message += " Restart the controller to apply the new concurrency."
                self._restart_required = True
            if applied_to_form:
                self._dirty_sections.discard("downloads")
            else:
                message += " Newer edits remain unsaved."
            self._sync_settings_state("Saved")
            self.statusBar().showMessage(message, 8000)

        def _network_mode_changed(self, *_args: Any) -> None:
            if not hasattr(self, "custom_proxy"):
                return
            mode = str(self.network_mode.currentData())
            is_tor = mode == "tor"
            is_proxy = mode == "proxy"
            self.tor_proxy_label.setVisible(is_tor)
            self.tor_proxy_input.setVisible(is_tor)
            self.custom_proxy_label.setVisible(is_proxy)
            self.custom_proxy.setVisible(is_proxy)
            descriptions = {
                "direct": (
                    "Direct mode connects without a proxy. Ateable still uses conservative pacing, retries, and cooldowns."
                ),
                "tor": (
                    "Tor mode fails closed if the configured SOCKS endpoint is unavailable. Local controllers apply saved policy after restart."
                ),
                "proxy": (
                    "Use an operator-approved HTTP(S) or SOCKS proxy. Credentialed URLs are secrets and are never shown in diagnostics."
                ),
            }
            self.network_help.setText(descriptions.get(mode, ""))
            self.network_status.setText("Not probed")
            self.network_status.setProperty("state", "idle")
            self._repolish(self.network_status)

        def save_network_settings(self) -> None:
            mode = str(self.network_mode.currentData())
            tor_proxy = self.tor_proxy_input.text().strip()
            custom_proxy = self.custom_proxy.text().strip()
            try:
                proxy_for_mode(mode, custom_proxy, tor_proxy)
            except ValueError as exc:
                QMessageBox.warning(self, APP_NAME, str(exc))
                return
            if self.device_config is not None:
                try:
                    self.device_config = self.device_config.updated(
                        network_mode=mode,
                        proxy=custom_proxy if mode == "proxy" else None,
                        tor_proxy=tor_proxy,
                    )
                    self.device_config.save()
                    # Proxy URLs can contain credentials. Keep them only in the
                    # permission-restricted device configuration.
                    self.local_settings.remove("network_mode")
                    self.local_settings.remove("custom_proxy")
                except (OSError, ValueError) as exc:
                    QMessageBox.warning(self, APP_NAME, str(exc))
                    return
            else:
                self.local_settings.setValue("network_mode", mode)
                self.local_settings.setValue("custom_proxy", custom_proxy)
            message = "Network settings saved."
            if should_manage_local_controller(self.device_config):
                if self.controller_runtime is not None:
                    message += " Restart the app to apply the controller network mode."
                else:
                    message += " Restart the controller to apply its network mode."
                self._restart_required = True
            else:
                # Client-only devices have no embedded worker whose startup
                # policy could disagree with the new batch proxy.
                self.active_network_mode = mode
                self.active_tor_proxy = tor_proxy
                self.active_custom_proxy = custom_proxy
            self._dirty_sections.discard("network")
            self._sync_settings_state("Saved")
            self.statusBar().showMessage(message, 10000)

        def probe_network(self) -> None:
            if self._probe_in_flight:
                return
            mode = str(self.network_mode.currentData())
            try:
                proxy = proxy_for_mode(
                    mode,
                    self.custom_proxy.text(),
                    self.tor_proxy_input.text(),
                )
            except ValueError as exc:
                QMessageBox.warning(self, APP_NAME, str(exc))
                return
            self._probe_in_flight = True
            self.probe_network_button.setEnabled(False)
            self.network_status.setText("Probing…")
            self.network_status.setProperty("state", "idle")
            self._repolish(self.network_status)
            client = self.client

            def success(payload: Any) -> None:
                if isinstance(payload, dict):
                    status = "Reachable" if payload.get("reachable") else "Unreachable"
                    detail = payload.get("message") or ""
                    self.network_status.setText(
                        f"{status}{': ' + str(detail) if detail else ''}"
                    )
                    state = "success" if payload.get("reachable") else "error"
                else:
                    self.network_status.setText("Unreachable")
                    state = "error"
                self.network_status.setProperty("state", state)
                self._repolish(self.network_status)
                self._set_connection_state("online")

            def failed(error: str) -> None:
                self.network_status.setText(f"Probe unavailable: {error}")
                self.network_status.setProperty("state", "error")
                self._repolish(self.network_status)
                self._report_error(error)

            self._run_async(
                lambda: client.probe_network(mode, proxy),
                success,
                on_error=failed,
                on_finished=self._network_probe_finished,
            )

        def _network_probe_finished(self) -> None:
            self._probe_in_flight = False
            self.probe_network_button.setEnabled(True)

        def shutdown_controller(self) -> None:
            provisioner = self._ssh_provisioner
            self._ssh_provisioner = None
            if provisioner is not None:
                provisioner.stop_all()
            runtime = self.controller_runtime
            self.controller_runtime = None
            if runtime is not None:
                runtime.stop()

        def closeEvent(self, event: Any) -> None:
            self._closing = True
            self.poll_timer.stop()
            self.job_search_timer.stop()
            self.local_settings.setValue("window_geometry", self.saveGeometry())
            active = self.tabs.currentWidget()
            name = next(
                (key for key, widget in self._tab_names.items() if widget is active),
                "start",
            )
            self.local_settings.setValue("active_tab_name", name)
            self.local_settings.sync()
            self.shutdown_controller()
            super().closeEvent(event)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["--capture-to-bucket"]:
        from .capture_pc import main as capture_main

        return capture_main(arguments[1:])
    if "--help" in arguments or "-h" in arguments:
        parser = argparse.ArgumentParser(
            prog="Ateable",
            description="Open the Ateable admin desktop. Use ateable for the full admin CLI.",
        )
        parser.add_argument(
            "--capture-to-bucket",
            action="store_true",
            help="Capture URL-list metadata without opening the desktop",
        )
        if sys.stdout is not None:
            parser.print_help()
        return 0
    if not PYSIDE_AVAILABLE:
        print(
            "ATEABLE desktop requires PySide6. Install it with: python -m pip install PySide6",
            file=sys.stderr,
        )
        if PYSIDE_IMPORT_ERROR:
            print(f"Import error: {PYSIDE_IMPORT_ERROR}", file=sys.stderr)
        return 2
    app = QApplication([sys.argv[0], *arguments])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    app.setStyle("Fusion")
    window = AteableWindow()
    app.aboutToQuit.connect(window.shutdown_controller)
    window.show()
    try:
        return app.exec()
    finally:
        window.shutdown_controller()


if __name__ == "__main__":
    raise SystemExit(main())
