"""Provision and supervise Linux workers through non-interactive OpenSSH.

Only application source and control traffic cross the controller's connection.
Package installation, downloads and output storage happen on the worker VM.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .pairing import decode_pair_key
from .runtime_paths import worker_source_directory

_READY_PREFIX = b"ATEABLE_SSH_READY "
_MAX_SOURCE_BYTES = 32 * 1024 * 1024
_MAX_SSH_OUTPUT_BYTES = 4 * 1024 * 1024
_STDIN_CHECK_MARKER = b"ATEABLE_SSH_STDIN_V1"
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,253}\Z")
_USER = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")


class SshDeviceError(RuntimeError):
    """An actionable error that contains no raw SSH output or credentials."""


def _check_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise ValueError(f"{label} must be non-empty text without control characters")


def _remote_path(value: str, label: str) -> None:
    _check_text(value, label)
    if not value.startswith(("/", "~/")) or value.rstrip("/") in {"", "~"}:
        raise ValueError(f"{label} must be an absolute Linux path or start with ~/")
    if ".." in value.split("/"):
        raise ValueError(f"{label} must not contain parent directory components")


@dataclass(frozen=True, slots=True)
class SshDeviceSpec:
    host: str
    user: str | None = None
    port: int | None = None
    identity_file: str | None = None
    remote_dir: str = "~/.local/share/ateable-worker"
    concurrency: int = 1
    network_mode: str = "direct"
    proxy: str | None = field(default=None, repr=False)
    output_dir: str | None = None
    tunnel_port: int | None = None
    s3_endpoint: str | None = None
    s3_profile: str | None = None
    workspace_id: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if self.workspace_id is not None and (
            not isinstance(self.workspace_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.workspace_id)
        ):
            raise ValueError("Workspace identifier contains unsupported characters")
        if not isinstance(self.host, str) or not _HOST.fullmatch(self.host):
            # OpenSSH also accepts bare IPv6 literals as the destination argument.
            import ipaddress

            try:
                ipaddress.IPv6Address(self.host)
            except (ValueError, TypeError):
                raise ValueError(
                    "SSH host must be a hostname, IP or SSH config alias"
                ) from None
        if self.user is not None and (
            not isinstance(self.user, str) or not _USER.fullmatch(self.user)
        ):
            raise ValueError("SSH user contains unsupported characters")
        for value, label in (
            (self.port, "SSH port"),
            (self.tunnel_port, "Tunnel port"),
        ):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= 65535
            ):
                raise ValueError(f"{label} must be between 1 and 65535")
        if (
            isinstance(self.concurrency, bool)
            or not isinstance(self.concurrency, int)
            or not 1 <= self.concurrency <= 64
        ):
            raise ValueError("Worker concurrency must be between 1 and 64")
        _remote_path(self.remote_dir, "Worker directory")
        if self.output_dir is not None:
            _remote_path(self.output_dir, "Output directory")
        if self.identity_file is not None:
            _check_text(self.identity_file, "SSH identity path")
        if self.s3_profile is not None:
            _check_text(self.s3_profile, "Worker S3 profile")
            if any(127 <= ord(character) <= 159 for character in self.s3_profile):
                raise ValueError(
                    "Worker S3 profile must not contain control characters"
                )
        if self.s3_endpoint is not None:
            _check_text(self.s3_endpoint, "Worker S3 endpoint")
            try:
                endpoint = urlsplit(self.s3_endpoint)
                valid_endpoint = (
                    endpoint.scheme in {"http", "https"}
                    and bool(endpoint.hostname)
                    and endpoint.username is None
                    and endpoint.password is None
                    and not endpoint.query
                    and not endpoint.fragment
                    and not any(
                        character.isspace() or 127 <= ord(character) <= 159
                        for character in self.s3_endpoint
                    )
                )
                _ = endpoint.port
            except ValueError:
                valid_endpoint = False
            if not valid_endpoint:
                raise ValueError(
                    "Worker S3 endpoint must be HTTP(S) without credentials, a query, a fragment or control characters"
                )
        if self.network_mode not in {"direct", "proxy", "tor"}:
            raise ValueError("Worker network mode must be direct, proxy or tor")
        if self.network_mode == "proxy" and not self.proxy:
            raise ValueError("Proxy mode requires a proxy URL")
        if self.proxy:
            _check_text(self.proxy, "Proxy URL")
            try:
                parsed = urlsplit(self.proxy)
                valid = parsed.scheme in {
                    "http",
                    "https",
                    "socks4",
                    "socks4a",
                    "socks5",
                    "socks5h",
                } and bool(parsed.hostname)
                _ = parsed.port
            except ValueError:
                valid = False
            if not valid or (self.network_mode == "tor" and parsed.scheme != "socks5h"):
                raise ValueError(
                    "Worker proxy must be an HTTP or SOCKS URL; Tor requires socks5h"
                )

    @property
    def connection_key(self) -> str:
        """Stable identity for one deployed worker, independent of secrets."""
        value = (self.host, self.user, self.port, self.remote_dir)
        return hashlib.sha256(json.dumps(value).encode()).hexdigest()

    @property
    def reverse_port(self) -> int:
        return self.tunnel_port or (20000 + int(self.connection_key[:8], 16) % 40000)


@dataclass(frozen=True, slots=True)
class SshInstallation:
    remote_dir: str
    python_version: str


def ssh_command(
    spec: SshDeviceSpec,
    command: str,
    *,
    reverse_port: int | None = None,
    controller_port: int | None = None,
    controller_host: str = "127.0.0.1",
) -> list[str]:
    """Build an argv list. Dynamic paths and credentials never enter a shell."""
    arguments = [
        _ssh_executable(),
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "LogLevel=ERROR",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ForwardX11=no",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "RemoteCommand=none",
        "-o",
        "SessionType=default",
    ]
    if spec.port is not None:
        arguments.extend(["-p", str(spec.port)])
    if spec.user:
        arguments.extend(["-l", spec.user])
    if spec.identity_file:
        arguments.extend(["-i", str(Path(spec.identity_file).expanduser().resolve())])
    if reverse_port is not None:
        if (
            controller_port is None
            or not 1 <= controller_port <= 65535
            or not 1 <= reverse_port <= 65535
        ):
            raise ValueError(
                "Reverse forwarding requires valid controller and remote ports"
            )
        if controller_host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                "The SSH controller must be on the local loopback interface"
            )
        target = "[::1]" if controller_host == "::1" else controller_host
        arguments.extend(["-R", f"127.0.0.1:{reverse_port}:{target}:{controller_port}"])
    else:
        arguments.extend(["-o", "ClearAllForwardings=yes"])
    arguments.extend(["--", spec.host, command])
    return arguments


def _is_windows() -> bool:
    return os.name == "nt"


def _ssh_executable() -> str:
    """Prefer Git's pipe-capable Windows client without changing user settings."""
    explicit = os.environ.get("ATEABLE_SSH_EXECUTABLE")
    if explicit is not None:
        try:
            path = Path(explicit)
            valid = (
                bool(explicit)
                and not any(ord(character) < 32 for character in explicit)
                and path.is_absolute()
                and path.is_file()
                and not path.is_symlink()
                and (
                    path.suffix.lower() == ".exe"
                    if _is_windows()
                    else os.access(path, os.X_OK)
                )
            )
        except (OSError, ValueError):
            valid = False
        if not valid:
            raise SshDeviceError(
                "ATEABLE_SSH_EXECUTABLE must name an absolute, existing SSH executable file."
            )
        return str(path)
    if _is_windows():
        candidates = []
        for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
            root = os.environ.get(variable)
            if root:
                candidates.append(Path(root) / "Git" / "usr" / "bin" / "ssh.exe")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.extend(
                (
                    Path(local) / "Programs" / "Git" / "usr" / "bin" / "ssh.exe",
                    Path(local) / "Git" / "usr" / "bin" / "ssh.exe",
                )
            )
        for candidate in candidates:
            if candidate.is_file() and not candidate.is_symlink():
                return str(candidate)
        return shutil.which("ssh") or "ssh"
    return "ssh"


def _process_options() -> dict[str, Any]:
    # Windows is the normal controller platform. Do not create terminal windows.
    if _is_windows():
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _failure(stage: str, diagnostic: bytes = b"") -> SshDeviceError:
    """Classify useful errors without echoing arbitrary remote output."""
    lowered = diagnostic.lower()
    if (
        b"host key verification failed" in lowered
        or b"remote host identification has changed" in lowered
        or b"no ed25519 host key is known" in lowered
    ):
        detail = (
            "Trust or verify this VM's SSH host key using your normal SSH client first."
        )
    elif b"permission denied" in lowered or b"no such identity" in lowered:
        detail = (
            "Check the SSH user, key path and SSH agent; password prompts are disabled."
        )
    elif (
        b"remote port forwarding failed" in lowered
        or b"administratively prohibited" in lowered
    ):
        detail = (
            "Enable SSH remote forwarding and choose an unused tunnel port on the VM."
        )
    elif b"ateable_unsupported_os" in lowered:
        detail = "Automatic SSH provisioning currently supports Linux VMs only."
    elif (
        b"ateable_no_privileges" in lowered
        or b"password is required" in lowered
        or b"not allowed to execute" in lowered
    ):
        detail = "Install Python 3.11+, python3-venv and FFmpeg on the VM, or allow sudo without a password for its package manager."
    elif b"ateable_python_version" in lowered:
        detail = "This VM needs Python 3.11 or later. Its system packages did not provide that version."
    elif b"ateable_js_runtime" in lowered:
        detail = "Install Deno 2.3+ or Node.js 22+ on the VM, or check its access to official Deno releases."
    elif b"ateable_unsupported_packages" in lowered:
        detail = "Install Python 3.11+, venv, pip and FFmpeg on the VM, then reconnect."
    elif b"ateable_worker_busy" in lowered:
        detail = "A worker already owns this VM directory; stop that worker before reconnecting."
    elif b"ateable_not_installed" in lowered:
        detail = "Provision the worker on this VM before connecting."
    elif b"ateable_pair_failed" in lowered:
        detail = "The VM could not authenticate with the controller. Check the tunnel and create a fresh pairing key if its saved credentials are no longer valid."
    elif (
        b"connection refused" in lowered
        or b"connection timed out" in lowered
        or b"could not resolve hostname" in lowered
    ):
        detail = "Check the VM address, SSH service and network connection."
    else:
        detail = (
            "Check SSH key access, VM connectivity and its Python/package installation."
        )
    return SshDeviceError(f"{stage}: {detail}")


_INSTALL_SYSTEM = r"""set -eu
if [ "$(uname -s)" != Linux ]; then echo ATEABLE_UNSUPPORTED_OS >&2; exit 61; fi
if command -v python3 >/dev/null 2>&1 && command -v ffmpeg >/dev/null 2>&1 && python3 -c 'import sys, venv, ensurepip; assert sys.version_info >= (3, 11)' >/dev/null 2>&1 && { [ "${ATEABLE_REQUIRE_TOR:-0}" = 0 ] || command -v tor >/dev/null 2>&1; }; then
    exit 0
fi
tor_package=""
if [ "${ATEABLE_REQUIRE_TOR:-0}" = 1 ]; then tor_package="tor"; fi
privilege=""
if [ "$(id -u)" != 0 ]; then
    if ! command -v sudo >/dev/null 2>&1; then
        echo ATEABLE_NO_PRIVILEGES >&2; exit 62
    fi
    privilege="sudo -n"
fi
if command -v apt-get >/dev/null 2>&1; then
    $privilege env DEBIAN_FRONTEND=noninteractive apt-get update -qq
    $privilege env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv python3-pip ffmpeg $tor_package
elif command -v dnf >/dev/null 2>&1; then
    $privilege dnf install -y python3 python3-pip ffmpeg $tor_package
elif command -v apk >/dev/null 2>&1; then
    $privilege apk add --no-cache python3 py3-pip ffmpeg $tor_package
elif command -v pacman >/dev/null 2>&1; then
    # Use the existing package database on an already updated Arch system.
    # Never refresh it alone or silently perform a full system upgrade.
    $privilege pacman --sync --needed --noconfirm python python-pip ffmpeg $tor_package
else
    echo ATEABLE_UNSUPPORTED_PACKAGES >&2; exit 63
fi
if ! python3 -c 'import sys, venv, ensurepip; assert sys.version_info >= (3, 11)' >/dev/null 2>&1; then
    echo ATEABLE_PYTHON_VERSION >&2; exit 64
fi
command -v ffmpeg >/dev/null 2>&1
"""

_DEPLOY_SOURCE = r"""
import fcntl, json, os, pathlib, subprocess, sys, tarfile, tempfile, venv
settings = json.loads(sys.stdin.buffer.readline())
base = pathlib.Path(settings["remote_dir"]).expanduser().resolve()
if base == pathlib.Path("/") or base == pathlib.Path.home():
    raise SystemExit("ATEABLE_INVALID_DIRECTORY")
base.mkdir(parents=True, mode=0o700, exist_ok=True)
lock = (base / "install.lock").open("a")
try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit("ATEABLE_WORKER_BUSY")
runtime_lock = (base / "worker.lock").open("a")
try:
    fcntl.flock(runtime_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit("ATEABLE_WORKER_BUSY")
os.umask(0o077)
with tempfile.TemporaryDirectory(prefix="source-", dir=base) as temporary:
    root = pathlib.Path(temporary)
    total = 0
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz") as archive:
        for member in archive:
            path = pathlib.PurePosixPath(member.name)
            if not member.isfile() or path.is_absolute() or ".." in path.parts:
                raise SystemExit("ATEABLE_INVALID_SOURCE")
            if path.parts[0] != "ateable" and member.name not in {"pyproject.toml", "LICENSE", "THIRD_PARTY_NOTICES.md"}:
                raise SystemExit("ATEABLE_INVALID_SOURCE")
            total += member.size
            if total > 33554432:
                raise SystemExit("ATEABLE_SOURCE_TOO_LARGE")
            target = root / pathlib.Path(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, target.open("wb") as output:
                while True:
                    chunk = source.read(65536)
                    if not chunk:
                        break
                    output.write(chunk)
    environment = base / "venv"
    python = environment / "bin" / "python"
    if not python.is_file():
        venv.EnvBuilder(with_pip=True).create(environment)
    subprocess.run([str(python), "-m", "pip", "--isolated", "--disable-pip-version-check", "--no-input", "install", "--no-cache-dir", "--index-url", "https://pypi.org/simple", "--upgrade", str(root) + "[worker]"], check=True)
    subprocess.run([str(python), "-c", "import ateable.remote_worker"], check=True)
    subprocess.run([str(python), "-c", "import ateable.participant; from pathlib import Path; from ateable.node_launcher import install_node_launcher; import sys; install_node_launcher(Path(sys.argv[1]))", str(base)], check=True)
    subprocess.run([str(python), "-c", "from pathlib import Path; from ateable.worker_runtime import ensure_worker_runtime; import sys; ensure_worker_runtime(Path(sys.argv[1]))", str(base)], check=True)
print("ATEABLE_SSH_INSTALLED " + json.dumps({"remote_dir": str(base), "python_version": sys.version.split()[0]}), flush=True)
"""

_WORKER_SCRIPT = r"""
import json, logging, os, pathlib, sys
settings = json.loads(sys.stdin.buffer.readline())
os.umask(0o077)
base = pathlib.Path(settings["remote_dir"]).expanduser().resolve()
os.environ["PATH"] = str(base / "runtime" / "bin") + os.pathsep + os.environ.get("PATH", "")
if settings.get("s3_endpoint"):
    os.environ["ATEABLE_S3_ENDPOINT"] = settings["s3_endpoint"]
if settings.get("s3_profile"):
    os.environ["AWS_PROFILE"] = settings["s3_profile"]
    os.environ["ATEABLE_S3_PROFILE"] = settings["s3_profile"]
from ateable.remote_worker import RemoteWorker, WorkerConfig
from ateable.network import DEFAULT_TOR_PROXY
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
class ManagedWorker(RemoteWorker):
    def connect(self):
        try:
            super().connect()
            self.client.heartbeat(self.worker_id, self.worker_token, [])
        except Exception:
            raise SystemExit("ATEABLE_PAIR_FAILED") from None
        print("ATEABLE_SSH_READY " + json.dumps({"worker_id": self.worker_id, "heartbeat": True}), flush=True)
config = WorkerConfig(
    coordinator_url=settings["coordinator_url"],
    name=settings["name"], pair_key=settings.get("pair_key"),
    worker_id=None, worker_token=None,
    output_dir=pathlib.Path(settings.get("output_dir") or str(base / "downloads")).expanduser().resolve(),
    concurrency=settings["concurrency"], credentials_file=base / "worker-credentials.json",
    network_mode=settings["network_mode"], proxy=settings.get("proxy"),
    tor_proxy=settings.get("proxy") if settings["network_mode"] == "tor" and settings.get("proxy") else DEFAULT_TOR_PROXY,
    cookies_file=None,
)
if config.network_mode == "tor":
    from ateable.tor_manager import ensure_tor
    ensure_tor(config.tor_proxy)
ManagedWorker(config).run()
"""

_LAUNCH_WORKER = r"""
import fcntl, json, os, pathlib, signal, subprocess, sys, threading, time
settings = json.loads(sys.stdin.buffer.readline())
os.umask(0o077)
base = pathlib.Path(settings["remote_dir"]).expanduser().resolve()
python = base / "venv" / "bin" / "python"
if not python.is_file():
    raise SystemExit("ATEABLE_NOT_INSTALLED")
lock = (base / "worker.lock").open("a")
try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit("ATEABLE_WORKER_BUSY")
stopping = threading.Event()
for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
    signal.signal(signum, lambda *_: stopping.set())
def watch_input():
    # The controller owns the process lifetime. SSH/stdin EOF stops its worker.
    while sys.stdin.buffer.read(1):
        pass
    stopping.set()
threading.Thread(target=watch_input, daemon=True).start()
child = subprocess.Popen([str(python), "-u", "-c", WORKER_SCRIPT], stdin=subprocess.PIPE, start_new_session=True)
child.stdin.write((json.dumps(settings) + "\n").encode())
child.stdin.close()
try:
    while child.poll() is None and not stopping.wait(0.2):
        pass
finally:
    if child.poll() is None:
        os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
raise SystemExit(child.returncode or 0)
""".replace("WORKER_SCRIPT", repr(_WORKER_SCRIPT))


class SshWorkerHandle:
    """A managed tunnel/worker process; readiness follows an API heartbeat."""

    def __init__(self, process: subprocess.Popen[bytes], spec: SshDeviceSpec):
        self.process = process
        self.spec = spec
        self.worker_id = ""
        self._ready = threading.Event()
        self._diagnostic = bytearray()
        self._diagnostic_lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._threads = [
            threading.Thread(
                target=self._read_stdout, name="ateable-ssh-status", daemon=True
            ),
            threading.Thread(
                target=self._read_stderr, name="ateable-ssh-errors", daemon=True
            ),
        ]
        for thread in self._threads:
            thread.start()

    def _read_stdout(self) -> None:
        if self.process.stdout is None:
            return
        for line in iter(lambda: self.process.stdout.readline(65536), b""):
            if not line.startswith(_READY_PREFIX):
                continue
            try:
                payload = json.loads(line[len(_READY_PREFIX) :])
                worker_id = payload["worker_id"]
                if (
                    payload.get("heartbeat") is not True
                    or not isinstance(worker_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", worker_id)
                ):
                    continue
            except (ValueError, KeyError, TypeError):
                continue
            self.worker_id = worker_id
            self._ready.set()

    def _read_stderr(self) -> None:
        if self.process.stderr is None:
            return
        for chunk in iter(lambda: self.process.stderr.read(4096), b""):
            with self._diagnostic_lock:
                self._diagnostic.extend(chunk)
                del self._diagnostic[:-65536]

    def wait_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if (
                self._ready.wait(min(0.1, max(0, deadline - time.monotonic())))
                and self.poll() is None
            ):
                return
            if self.poll() is not None:
                # The stderr drain usually finishes immediately once SSH exits.
                self._threads[1].join(timeout=0.2)
                with self._diagnostic_lock:
                    raise _failure(
                        "SSH worker did not connect", bytes(self._diagnostic)
                    )
        with self._diagnostic_lock:
            diagnostic = bytes(self._diagnostic)
        raise _failure("SSH worker handshake timed out", diagnostic)

    def poll(self) -> int | None:
        return self.process.poll()

    def stop(self) -> None:
        with self._stop_lock:
            if self.process.stdin is not None and not self.process.stdin.closed:
                try:
                    self.process.stdin.close()
                except OSError:
                    pass
            if self.poll() is None:
                # EOF gives the remote supervisor time to terminate its group.
                try:
                    self.process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=3)
            for thread in self._threads:
                thread.join(timeout=0.2)
            for stream in (self.process.stdout, self.process.stderr):
                if stream is not None:
                    stream.close()


class SshProvisioner:
    """One owner for pending SSH operations and running worker handles."""

    def __init__(self, source_dir: Path | str | None = None):
        self.source_dir = (
            Path(source_dir) if source_dir else worker_source_directory(__file__)
        )
        self._handles: dict[str, SshWorkerHandle] = {}
        self._pending: set[subprocess.Popen[bytes]] = set()
        self._lock = threading.RLock()
        self._device_locks: dict[str, threading.RLock] = {}
        self._closed = threading.Event()

    def _check_open(self) -> None:
        if self._closed.is_set():
            raise SshDeviceError("SSH manager has been closed")

    def _device_lock(self, spec: SshDeviceSpec) -> threading.RLock:
        with self._lock:
            return self._device_locks.setdefault(spec.connection_key, threading.RLock())

    def _start(
        self,
        arguments: list[str],
        *,
        stdout: Any = subprocess.PIPE,
        stderr: Any = subprocess.PIPE,
    ) -> subprocess.Popen[bytes]:
        with self._lock:
            self._check_open()
            try:
                process = subprocess.Popen(
                    arguments,
                    stdin=subprocess.PIPE,
                    stdout=stdout,
                    stderr=stderr,
                    **_process_options(),
                )
            except FileNotFoundError:
                raise SshDeviceError(
                    "OpenSSH client was not found. Install or enable the SSH client on the controller."
                ) from None
            except OSError:
                raise SshDeviceError(
                    "The controller could not start its SSH client"
                ) from None
            self._pending.add(process)
            return process

    def _run(
        self,
        spec: SshDeviceSpec,
        command: str,
        input_data: bytes,
        *,
        stage: str,
        timeout: float,
    ) -> bytes:
        with ExitStack() as resources:
            output_file = errors_file = None
            if _is_windows():
                # Windows OpenSSH can keep a completed session alive when its
                # stdout is an anonymous pipe. One-shot commands use private,
                # automatically deleted files; live worker status keeps pipes.
                output_file = resources.enter_context(tempfile.TemporaryFile())
                errors_file = resources.enter_context(tempfile.TemporaryFile())
                process = self._start(
                    ssh_command(spec, command), stdout=output_file, stderr=errors_file
                )
            else:
                process = self._start(ssh_command(spec, command))
            try:
                try:
                    output, errors = process.communicate(input_data, timeout=timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    if _is_windows():
                        raise SshDeviceError(
                            f"{stage} timed out. Check VM connectivity and package downloads. "
                            "If the Windows SSH client stalls with redirected input, install "
                            "Git for Windows or set ATEABLE_SSH_EXECUTABLE to its ssh.exe, "
                            "then restart Ateable."
                        ) from None
                    raise _failure(f"{stage} timed out") from None
                except OSError:
                    # A broken stdin/capture stream must not orphan a remote
                    # installer after this manager stops tracking its SSH child.
                    if process.poll() is None:
                        try:
                            process.kill()
                        except OSError:
                            pass
                    try:
                        process.wait(timeout=5)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                    for stream in (process.stdin, process.stdout, process.stderr):
                        if stream is not None:
                            try:
                                stream.close()
                            except OSError:
                                pass
                    raise _failure(stage) from None
                self._check_open()
                if output_file is not None and errors_file is not None:
                    output_file.seek(0)
                    output = output_file.read(_MAX_SSH_OUTPUT_BYTES + 1)
                    errors_file.seek(0, os.SEEK_END)
                    errors_file.seek(max(0, errors_file.tell() - _MAX_SSH_OUTPUT_BYTES))
                    errors = errors_file.read(_MAX_SSH_OUTPUT_BYTES)
                if process.returncode:
                    raise _failure(stage, errors)
                if len(output) > _MAX_SSH_OUTPUT_BYTES:
                    raise _failure(f"{stage} exceeded the response size limit")
                return output
            finally:
                with self._lock:
                    self._pending.discard(process)

    def source_archive(self) -> bytes:
        """Package only executable source/metadata, never state or downloads."""
        required = [
            self.source_dir / name
            for name in ("pyproject.toml", "LICENSE", "THIRD_PARTY_NOTICES.md")
        ]
        package = self.source_dir / "ateable"
        if (
            any(not path.is_file() or path.is_symlink() for path in required)
            or not package.is_dir()
            or package.is_symlink()
        ):
            raise SshDeviceError(
                "SSH provisioning needs the backend source package, pyproject.toml and license notices. Set its source directory explicitly for a packaged controller."
            )
        paths = required + sorted(package.rglob("*.py"))
        logo = package / "assets/logo-white-on-black.png"
        if logo.is_file() and not logo.is_symlink():
            paths.append(logo)
        total = 0
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as archive:
            for path in paths:
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or "__pycache__" in path.parts
                    or any(
                        part.startswith(".")
                        for part in path.relative_to(self.source_dir).parts
                    )
                ):
                    continue
                total += path.stat().st_size
                if total > _MAX_SOURCE_BYTES:
                    raise SshDeviceError(
                        "Worker source package exceeds the 32 MiB upload limit"
                    )
                archive.add(
                    path,
                    arcname=path.relative_to(self.source_dir).as_posix(),
                    recursive=False,
                )
        return data.getvalue()

    def _check_stdin(self, spec: SshDeviceSpec) -> None:
        if not _is_windows():
            return
        # Requires only POSIX sh, before Python or system packages are installed.
        # Reading a line and echoing it proves input delivery without a secret,
        # archive upload, filesystem write, or dependency on stdin reaching EOF.
        output = self._run(
            spec,
            "sh -c 'IFS= read -r value && printf %s \"$value\"'",
            _STDIN_CHECK_MARKER + b"\n",
            stage="Windows SSH input check",
            timeout=12,
        )
        if output != _STDIN_CHECK_MARKER:
            raise SshDeviceError(
                "The Windows SSH client did not deliver the expected input. "
                "Install Git for Windows or set ATEABLE_SSH_EXECUTABLE to a working "
                "ssh.exe, then restart Ateable."
            )

    def provision(
        self, spec: SshDeviceSpec, progress: Callable[[str], None] | None = None
    ) -> SshInstallation:
        with self._device_lock(spec):
            self._check_open()
            with self._lock:
                current = self._handles.get(spec.connection_key)
                if current is not None and current.poll() is None:
                    return SshInstallation(spec.remote_dir, "already connected")
            self._check_stdin(spec)
            archive = self.source_archive()
            if progress:
                progress(
                    "Checking Linux VM and installing Python/FFmpeg dependencies on the VM"
                )
            installer = (
                "ATEABLE_REQUIRE_TOR="
                + ("1" if spec.network_mode == "tor" and not spec.proxy else "0")
                + "\n"
                + _INSTALL_SYSTEM
            )
            self._run(
                spec,
                "sh -s",
                installer.encode(),
                stage="VM dependency installation failed",
                timeout=900,
            )
            if progress:
                progress(
                    "Uploading Ateable source and installing its Python dependencies on the VM"
                )
            payload = (
                json.dumps({"remote_dir": spec.remote_dir}).encode() + b"\n" + archive
            )
            result = self._run(
                spec,
                "python3 -c " + shlex.quote(_DEPLOY_SOURCE),
                payload,
                stage="Worker deployment failed",
                timeout=900,
            )
            for line in reversed(result.splitlines()):
                if line.startswith(b"ATEABLE_SSH_INSTALLED "):
                    try:
                        details = json.loads(line[len(b"ATEABLE_SSH_INSTALLED ") :])
                        return SshInstallation(
                            str(details["remote_dir"]), str(details["python_version"])
                        )
                    except (ValueError, KeyError, TypeError):
                        break
            raise SshDeviceError(
                "VM installation finished without confirming a usable worker"
            )

    def connect(
        self,
        spec: SshDeviceSpec,
        controller_url: str,
        pair_key: str | None = None,
        timeout: float = 30,
    ) -> SshWorkerHandle:
        """Connect a provisioned worker; omit pair_key to reuse its credentials."""
        with self._device_lock(spec):
            self._check_open()
            with self._lock:
                existing = self._handles.get(spec.connection_key)
                if existing is not None and existing.poll() is None:
                    if existing.spec != spec:
                        raise SshDeviceError(
                            "This VM is already connected. Stop its worker before changing SSH or download settings."
                        )
                    return existing
                if existing is not None:
                    existing.stop()
                    self._handles.pop(spec.connection_key, None)
            try:
                controller = urlsplit(controller_url)
                port = controller.port or 80
            except ValueError:
                raise ValueError("Controller URL has an invalid port") from None
            if (
                controller.scheme != "http"
                or controller.hostname not in {"127.0.0.1", "localhost", "::1"}
                or controller.username
                or controller.password
                or controller.path not in {"", "/"}
                or controller.query
                or controller.fragment
            ):
                raise ValueError(
                    "SSH onboarding requires a local HTTP controller URL, such as http://127.0.0.1:8765"
                )
            if pair_key is not None:
                decode_pair_key(
                    pair_key
                )  # Reject malformed keys before any SSH launch.
            if timeout <= 0:
                raise ValueError("SSH handshake timeout must be positive")
            settings = {
                "remote_dir": spec.remote_dir,
                "output_dir": spec.output_dir,
                "coordinator_url": f"http://127.0.0.1:{spec.reverse_port}",
                "name": spec.host,
                "pair_key": pair_key,
                "concurrency": spec.concurrency,
                "network_mode": spec.network_mode,
                "proxy": spec.proxy,
                "s3_endpoint": spec.s3_endpoint,
                "s3_profile": spec.s3_profile,
            }
            command = "exec python3 -u -c " + shlex.quote(_LAUNCH_WORKER)
            process = self._start(
                ssh_command(
                    spec,
                    command,
                    reverse_port=spec.reverse_port,
                    controller_port=port,
                    controller_host=controller.hostname,
                )
            )
            handle = SshWorkerHandle(process, spec)
            try:
                assert process.stdin is not None
                process.stdin.write(json.dumps(settings).encode() + b"\n")
                process.stdin.flush()
                handle.wait_ready(timeout)
                with self._lock:
                    self._check_open()
                    self._handles[spec.connection_key] = handle
                    self._pending.discard(process)
                return handle
            except OSError:
                handle.stop()
                raise _failure("SSH worker could not start") from None
            except BaseException:
                handle.stop()
                raise
            finally:
                with self._lock:
                    self._pending.discard(process)

    def provision_and_connect(
        self,
        spec: SshDeviceSpec,
        controller_url: str,
        pair_key: str | None = None,
        *,
        timeout: float = 30,
        progress: Callable[[str], None] | None = None,
    ) -> SshWorkerHandle:
        with self._device_lock(spec):
            self.provision(spec, progress)
            if progress:
                progress("Opening the SSH tunnel and verifying the worker heartbeat")
            handle = self.connect(spec, controller_url, pair_key, timeout)
            if progress:
                progress(
                    "Worker is paired and online; downloads and files stay on the VM"
                )
            return handle

    def stop_all(self) -> None:
        """Close this manager, including operations started by background UI work."""
        with self._lock:
            self._closed.set()
            handles = list(self._handles.values())
            pending = list(self._pending)
            self._handles.clear()
        for process in pending:
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
        for handle in handles:
            handle.stop()

    close = stop_all


class SshDeviceStore:
    """Remember SSH paths/settings, with no pairing keys or worker tokens."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.RLock()

    def load(self) -> dict[str, SshDeviceSpec]:
        with self._lock:
            try:
                contents = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return {}
            except (OSError, ValueError, TypeError):
                raise SshDeviceError(
                    "Could not read saved SSH device settings"
                ) from None
            try:
                raw = contents["devices"]
                allowed = {item.name for item in fields(SshDeviceSpec)}
                if not isinstance(raw, dict):
                    raise TypeError
                result = {}
                for name, record in raw.items():
                    _check_text(name, "Saved device name")
                    if not isinstance(record, dict) or not set(record).issubset(
                        allowed
                    ):
                        raise ValueError
                    spec = SshDeviceSpec(**record)
                    self._check_proxy(spec)
                    result[name] = spec
                return result
            except (KeyError, TypeError, ValueError):
                raise SshDeviceError("Saved SSH device settings are invalid") from None

    @staticmethod
    def _check_proxy(spec: SshDeviceSpec) -> None:
        if spec.proxy:
            proxy = urlsplit(spec.proxy)
            if proxy.username is not None or proxy.password is not None:
                raise ValueError(
                    "Proxy credentials cannot be saved in SSH settings; supply them only for this session"
                )

    def _write(self, devices: dict[str, SshDeviceSpec]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        import tempfile

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=self.path.name + ".", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(
                    {
                        "version": 1,
                        "devices": {
                            name: asdict(spec) for name, spec in devices.items()
                        },
                    },
                    output,
                    indent=2,
                    ensure_ascii=False,
                )
                output.write("\n")
            temporary.chmod(0o600)
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def save(self, name: str, spec: SshDeviceSpec) -> None:
        _check_text(name, "Saved device name")
        self._check_proxy(spec)
        with self._lock:
            devices = self.load()
            devices[name] = spec
            self._write(devices)

    def remove(self, name: str) -> None:
        with self._lock:
            devices = self.load()
            if name in devices:
                del devices[name]
                self._write(devices)
