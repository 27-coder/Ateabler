"""Managed local Tor process lifecycle for fail-closed worker networking."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

from platformdirs import user_state_path

from .device_config import default_config_dir
from .network import NetworkStatus, probe_network, redact_proxy


class TorError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TorStatus:
    managed: bool
    running: bool
    bootstrapped: bool
    proxy: str
    executable: str | None
    pid: int | None
    torrc: str | None
    data_dir: str | None
    message: str

    def to_dict(self) -> dict[str, str | int | bool | None]:
        return asdict(self)


def _state_dir() -> Path:
    override = os.getenv("ATEABLE_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve() / "tor"
    return Path(user_state_path("Ateable", appauthor=False)) / "tor"


def find_tor() -> str | None:
    return shutil.which("tor") or shutil.which("tor.exe")


def _local_proxy(proxy: str) -> tuple[str, int]:
    parsed = urlparse(proxy)
    if (
        parsed.scheme != "socks5h"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or not parsed.port
    ):
        raise TorError(
            "Managed Tor requires ATEABLE_TOR_PROXY="
            "socks5h://127.0.0.1:<port>; external Tor endpoints are only probed"
        )
    return parsed.hostname, parsed.port


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def tor_status(
    proxy: str, *, timeout: float = 10, verify_circuit: bool = True
) -> TorStatus:
    executable = find_tor()
    state = _state_dir()
    pid_file = state / "process.json"
    pid: int | None = None
    managed = False
    try:
        process = json.loads(pid_file.read_text(encoding="utf-8"))
        pid = int(process["pid"])
        managed = _process_alive(pid)
    except (OSError, ValueError, TypeError, KeyError):
        pass
    log_path = state / "tor.log"
    logged_bootstrap = False
    try:
        logged_bootstrap = "Bootstrapped 100% (done)" in log_path.read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        pass
    if verify_circuit and (logged_bootstrap or not managed):
        network: NetworkStatus = probe_network(
            "tor", tor_proxy=proxy, timeout=timeout
        )
        verified = network.reachable
    else:
        # During bootstrap/setup, a cheap socket check avoids repeatedly hitting
        # the external verification endpoint.
        network = probe_network("proxy", proxy=proxy, timeout=min(timeout, 1))
        verified = False
    bootstrapped = verified and (logged_bootstrap or not managed)
    return TorStatus(
        managed=managed,
        running=network.reachable,
        bootstrapped=network.reachable and bootstrapped,
        proxy=redact_proxy(proxy) or "socks5h://127.0.0.1:9050",
        executable=executable,
        pid=pid if managed else None,
        torrc=str(state / "torrc") if managed else None,
        data_dir=str(state / "data") if managed else None,
        message=(
            "Tor circuit is ready"
            if bootstrapped
            else network.message
        ),
    )


def ensure_tor(
    proxy: str,
    *,
    timeout: float = 180,
    cancel_event: threading.Event | None = None,
) -> TorStatus:
    """Ensure a verified Tor circuit, allowing GUI callers to cancel startup."""

    if cancel_event is not None and cancel_event.is_set():
        raise TorError("Tor bootstrap cancelled")
    parsed = urlparse(proxy)
    is_local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    probe_timeout = 2 if cancel_event is not None else 10
    current = tor_status(proxy, timeout=probe_timeout)
    if cancel_event is not None and cancel_event.is_set():
        raise TorError("Tor bootstrap cancelled")
    if current.running and (current.bootstrapped or not current.managed):
        return current
    if not is_local:
        raise TorError(
            f"Configured Tor endpoint is unavailable ({proxy}); refusing direct fallback"
        )
    _, port = _local_proxy(proxy)
    executable = find_tor()
    if not executable:
        raise TorError(
            "Tor mode is selected but no tor/tor.exe executable was found on PATH"
        )
    state = _state_dir()
    data_dir = state / "data"
    state.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    for directory in (state, data_dir, default_config_dir()):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o700)
        except OSError:
            pass
    torrc = state / "torrc"
    torrc.write_text(
        "\n".join(
            [
                f"SocksPort 127.0.0.1:{port}",
                f'DataDirectory "{data_dir}"',
                "CookieAuthentication 1",
                "AvoidDiskWrites 1",
                "ClientOnly 1",
                "SafeSocks 1",
                "TestSocks 1",
                "SafeLogging 1",
                "Log notice stdout",
                "",
            ]
        ),
        encoding="utf-8",
    )
    try:
        torrc.chmod(0o600)
    except OSError:
        pass
    log_path = state / "tor.log"
    log_handle = log_path.open("ab")
    creationflags = 0
    start_new_session = os.name != "nt"
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        process = subprocess.Popen(
            [executable, "-f", str(torrc)],
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=start_new_session,
            creationflags=creationflags,
        )
    except OSError as exc:
        log_handle.close()
        raise TorError(f"Could not start Tor: {exc}") from exc
    finally:
        log_handle.close()
    (state / "process.json").write_text(
        json.dumps({"pid": process.pid, "proxy": proxy}) + "\n",
        encoding="utf-8",
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            _terminate_process(process)
            raise TorError("Tor bootstrap cancelled")
        if process.poll() is not None:
            raise TorError(
                f"Tor exited before bootstrap (code {process.returncode}); "
                f"see {log_path}"
            )
        status = tor_status(proxy, timeout=probe_timeout)
        if status.bootstrapped:
            return status
        delay = 1 if status.running else 0.25
        if cancel_event is not None:
            if cancel_event.wait(delay):
                _terminate_process(process)
                raise TorError("Tor bootstrap cancelled")
        else:
            time.sleep(delay)
    _terminate_process(process)
    raise TorError(f"Tor did not bootstrap within {timeout:g}s; see {log_path}")


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        pass
