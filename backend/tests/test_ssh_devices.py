from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
from types import SimpleNamespace

import pytest

from ateable import ssh_devices
from ateable.pairing import encode_pair_key
from ateable.ssh_devices import (
    SshDeviceError,
    SshDeviceSpec,
    SshDeviceStore,
    SshProvisioner,
    ssh_command,
)


class FakeProcess:
    def __init__(
        self, *, stdout: bytes = b"", stderr: bytes = b"", returncode: int | None = None
    ):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = returncode
        self.input_data = b""
        self.terminated = False
        self.killed = False

    def communicate(self, input_data=None, timeout=None):
        self.input_data = input_data
        self.returncode = self.returncode or 0
        return self.stdout.read(), self.stderr.read()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = self.returncode or 0
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


def ready_process(worker_id="worker-one", *, heartbeat=True):
    payload = json.dumps({"worker_id": worker_id, "heartbeat": heartbeat}).encode()
    return FakeProcess(stdout=b"ATEABLE_SSH_READY " + payload + b"\n")


@pytest.fixture
def key():
    return encode_pair_key("http://127.0.0.1:8765", "key-one", "x" * 40)


@pytest.fixture
def capture_popen(monkeypatch):
    processes = []
    calls = []

    def factory(arguments, **kwargs):
        calls.append((arguments, kwargs))
        process = processes.pop(0)
        if kwargs["stdout"] != subprocess.PIPE:
            original = process.communicate

            def communicate(input_data=None, timeout=None):
                output, errors = original(input_data, timeout)
                kwargs["stdout"].write(output)
                kwargs["stderr"].write(errors)
                return None, None

            process.communicate = communicate
        return process

    monkeypatch.setattr(ssh_devices.subprocess, "Popen", factory)
    return processes, calls


@pytest.mark.parametrize(
    "value",
    [
        "-oProxyCommand=anything",
        "vm; touch /tmp/no",
        "vm@other",
        "vm\nother",
        "$(id)",
        "vm name",
    ],
)
def test_reject_ssh_option_and_shell_injection_in_host(value):
    with pytest.raises(ValueError, match="host"):
        SshDeviceSpec(value)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"user": "user; id"},
        {"port": 0},
        {"port": True},
        {"concurrency": 0},
        {"remote_dir": "/"},
        {"remote_dir": "~/../other"},
        {"remote_dir": "relative"},
        {"output_dir": "~another/Downloads"},
        {"identity_file": "key\nfile"},
        {"network_mode": "proxy"},
        {"network_mode": "tor", "proxy": "http://localhost:9050"},
    ],
)
def test_reject_invalid_worker_settings(kwargs):
    with pytest.raises(ValueError):
        SshDeviceSpec("worker-vm", **kwargs)


@pytest.mark.parametrize(
    "endpoint",
    [
        "ftp://objects.example",
        "https://access:secret@objects.example",
        "https://@objects.example",
        "https://objects.example?token=secret",
        "https://objects.example#secret",
        "https://objects.example\n/path",
        "https://objects.example/\x7f",
        "https://objects.example/\x85",
        " https://objects.example",
        "https://objects.example:wrong",
        "https:///missing-host",
        "",
    ],
)
def test_reject_secret_or_invalid_s3_endpoint(endpoint):
    with pytest.raises(ValueError, match="S3 endpoint"):
        SshDeviceSpec("worker-vm", s3_endpoint=endpoint)


@pytest.mark.parametrize(
    "profile", ["", "profile\nother", "profile\x00", "profile\x7f", "profile\x85"]
)
def test_reject_s3_profile_controls(profile):
    with pytest.raises(ValueError, match="S3 profile"):
        SshDeviceSpec("worker-vm", s3_profile=profile)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://objects.example",
        "http://minio.internal:9000",
        "https://objects.example/s3",
        "http://[::1]:9000",
    ],
)
def test_accept_nonsecret_s3_endpoint_and_profile(endpoint):
    spec = SshDeviceSpec(
        "worker-vm", s3_endpoint=endpoint, s3_profile="worker-downloads"
    )
    assert spec.s3_endpoint == endpoint
    assert spec.s3_profile == "worker-downloads"


def test_ssh_uses_key_config_alias_and_verified_host_without_shell(tmp_path):
    identity = tmp_path / "odd key $(touch nope)"
    spec = SshDeviceSpec(
        "worker-alias", user="vmuser", port=2222, identity_file=str(identity)
    )
    arguments = ssh_command(
        spec, "python3 -c 'pass'", reverse_port=23456, controller_port=8765
    )
    assert "BatchMode=yes" in arguments
    assert "StrictHostKeyChecking=yes" in arguments
    assert "ControlPath=none" in arguments
    assert "ForwardAgent=no" in arguments
    assert arguments[arguments.index("-i") + 1] == str(identity)
    assert arguments[arguments.index("-R") + 1] == "127.0.0.1:23456:127.0.0.1:8765"
    assert arguments[-3:] == ["--", "worker-alias", "python3 -c 'pass'"]
    assert "ClearAllForwardings=yes" in ssh_command(spec, "sh -s")


def test_ipv6_destination_supported():
    assert ssh_command(SshDeviceSpec("::1"), "true")[-2] == "::1"


@pytest.fixture
def windows_client_selection(monkeypatch):
    monkeypatch.setattr(ssh_devices, "_is_windows", lambda: True)
    for name in (
        "ATEABLE_SSH_EXECUTABLE",
        "ProgramW6432",
        "ProgramFiles",
        "ProgramFiles(x86)",
        "LOCALAPPDATA",
    ):
        monkeypatch.delenv(name, raising=False)


def test_windows_prefers_discovered_git_ssh_without_changing_path(
    windows_client_selection, monkeypatch, tmp_path
):
    git = tmp_path / "Program Files" / "Git" / "usr" / "bin" / "ssh.exe"
    git.parent.mkdir(parents=True)
    git.write_bytes(b"test executable metadata")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    monkeypatch.setenv("PATH", "unchanged-user-path")
    arguments = ssh_command(SshDeviceSpec("vm"), "sh -s")
    assert arguments[0] == str(git)
    assert "StrictHostKeyChecking=yes" in arguments
    assert "ForwardAgent=no" in arguments
    assert os.environ["PATH"] == "unchanged-user-path"


def test_windows_finds_per_user_git_and_falls_back_to_path(
    windows_client_selection, monkeypatch, tmp_path
):
    monkeypatch.setattr(ssh_devices.shutil, "which", lambda name: "/path/ssh.exe")
    assert ssh_devices._ssh_executable() == "/path/ssh.exe"
    git = tmp_path / "Programs" / "Git" / "usr" / "bin" / "ssh.exe"
    git.parent.mkdir(parents=True)
    git.touch()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert ssh_devices._ssh_executable() == str(git)


def test_windows_explicit_ssh_client_takes_precedence(
    windows_client_selection, monkeypatch, tmp_path
):
    chosen = tmp_path / "chosen ssh.exe"
    chosen.touch()
    monkeypatch.setenv("ATEABLE_SSH_EXECUTABLE", str(chosen))
    assert ssh_devices._ssh_executable() == str(chosen)


@pytest.mark.parametrize(
    "value", ["", "ssh.exe", "-oBad=yes", "missing.exe", "bad\n.exe"]
)
def test_explicit_ssh_client_rejects_invalid_paths_without_echoing_them(
    windows_client_selection, monkeypatch, value
):
    monkeypatch.setenv("ATEABLE_SSH_EXECUTABLE", value)
    with pytest.raises(SshDeviceError, match="absolute, existing SSH executable"):
        ssh_devices._ssh_executable()


@pytest.mark.parametrize("kind", ["directory", "text", "symlink"])
def test_windows_explicit_ssh_requires_regular_executable(
    windows_client_selection, monkeypatch, tmp_path, kind
):
    chosen = tmp_path / ("chosen.txt" if kind == "text" else "chosen.exe")
    if kind == "directory":
        chosen.mkdir()
    elif kind == "symlink":
        source = tmp_path / "source.exe"
        source.touch()
        try:
            chosen.symlink_to(source)
        except OSError:
            pytest.skip("Host cannot create symbolic links")
    else:
        chosen.touch()
    monkeypatch.setenv("ATEABLE_SSH_EXECUTABLE", str(chosen))
    with pytest.raises(SshDeviceError):
        ssh_devices._ssh_executable()


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable permission bits")
def test_posix_explicit_ssh_client_must_be_executable(monkeypatch, tmp_path):
    monkeypatch.setattr(ssh_devices, "_is_windows", lambda: False)
    chosen = tmp_path / "ssh"
    chosen.touch()
    chosen.chmod(0o600)
    monkeypatch.setenv("ATEABLE_SSH_EXECUTABLE", str(chosen))
    with pytest.raises(SshDeviceError):
        ssh_devices._ssh_executable()
    chosen.chmod(0o700)
    assert ssh_devices._ssh_executable() == str(chosen)


def test_source_upload_excludes_state_credentials_and_downloads(tmp_path):
    for name in ("pyproject.toml", "LICENSE", "THIRD_PARTY_NOTICES.md"):
        (tmp_path / name).write_text("metadata")
    package = tmp_path / "ateable"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "worker.py").write_text("print('source')")
    (package / "worker-credentials.json").write_text("secret")
    (package / "download.mp4").write_bytes(b"payload")
    (package / "linked.py").symlink_to(tmp_path / "LICENSE")
    (package / ".hidden.py").write_text("secret")
    archive = SshProvisioner(tmp_path).source_archive()
    with tarfile.open(fileobj=io.BytesIO(archive)) as uploaded:
        assert set(uploaded.getnames()) == {
            "pyproject.toml",
            "LICENSE",
            "THIRD_PARTY_NOTICES.md",
            "ateable/__init__.py",
            "ateable/worker.py",
        }
    assert b"payload" not in archive


def test_packaged_controller_uses_bundled_worker_source(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ssh_devices,
        "__file__",
        str(tmp_path / "installed" / "ateable" / "ssh_devices.py"),
    )
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "frozen"), raising=False)
    expected = tmp_path / "frozen" / "worker-source"
    assert SshProvisioner().source_dir == expected
    assert (
        SshProvisioner(tmp_path / "explicit-source").source_dir
        == tmp_path / "explicit-source"
    )


def test_provision_sends_remote_directory_as_data(capture_popen, tmp_path):
    processes, calls = capture_popen
    spec = SshDeviceSpec("vm", remote_dir="/tmp/worker $(touch BAD); 'quoted'")
    installer = FakeProcess(returncode=0)
    deployer = FakeProcess(
        stdout=b'ATEABLE_SSH_INSTALLED {"remote_dir":"/tmp/worker", "python_version":"3.12.1"}\n',
        returncode=0,
    )
    preflight = int(ssh_devices._is_windows())
    if preflight:
        processes.append(
            FakeProcess(stdout=ssh_devices._STDIN_CHECK_MARKER, returncode=0)
        )
    processes.extend([installer, deployer])
    messages = []
    result = SshProvisioner().provision(spec, messages.append)
    assert result.python_version == "3.12.1"
    assert len(messages) == 2
    assert calls[preflight][0][-1] == "sh -s"
    assert spec.remote_dir not in " ".join(calls[preflight + 1][0])
    line, archive = deployer.input_data.split(b"\n", 1)
    assert json.loads(line)["remote_dir"] == spec.remote_dir
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        assert "LICENSE" in source.getnames()
    assert b"pypi.org/simple" in calls[preflight + 1][0][-1].encode()
    assert all(call[1].get("shell") is None for call in calls)


def test_windows_stdin_preflight_uses_shell_only_and_bounded_marker(
    monkeypatch,
):
    monkeypatch.setattr(ssh_devices, "_is_windows", lambda: True)
    calls = []
    manager = SshProvisioner()

    def run(spec, command, input_data, *, stage, timeout):
        calls.append((command, input_data, stage, timeout))
        return ssh_devices._STDIN_CHECK_MARKER

    monkeypatch.setattr(manager, "_run", run)
    manager._check_stdin(SshDeviceSpec("vm"))
    assert len(calls) == 1
    command, payload, stage, timeout = calls[0]
    assert command.startswith("sh -c ") and "python" not in command
    assert payload == b"ATEABLE_SSH_STDIN_V1\n"
    assert "ATEABLE_SSH_STDIN_V1" not in command
    assert stage == "Windows SSH input check" and 0 < timeout <= 15


@pytest.mark.parametrize("output", [b"", b"wrong", b"ATEABLE_SSH_STDIN_V1\n"])
def test_windows_bad_stdin_stops_before_archive_or_dependency_install(
    monkeypatch, output
):
    monkeypatch.setattr(ssh_devices, "_is_windows", lambda: True)
    manager = SshProvisioner()
    calls = []

    def run(*args, **kwargs):
        calls.append(kwargs["stage"])
        return output

    monkeypatch.setattr(manager, "_run", run)
    monkeypatch.setattr(
        manager,
        "source_archive",
        lambda: pytest.fail("No upload after failed preflight"),
    )
    with pytest.raises(SshDeviceError, match="Git for Windows"):
        manager.provision(SshDeviceSpec("vm"))
    assert calls == ["Windows SSH input check"]


def test_non_windows_does_not_run_stdin_preflight(monkeypatch):
    monkeypatch.setattr(ssh_devices, "_is_windows", lambda: False)
    manager = SshProvisioner()
    monkeypatch.setattr(manager, "_run", lambda *_a, **_k: pytest.fail("Windows only"))
    manager._check_stdin(SshDeviceSpec("vm"))


@pytest.mark.parametrize(
    ("diagnostic", "expected"),
    [
        (b"Host key verification failed. PRIVATE_SECRET", "host key"),
        (
            b"Permission denied (publickey,password). PRIVATE_SECRET",
            "password prompts are disabled",
        ),
        (b"ATEABLE_NO_PRIVILEGES PRIVATE_SECRET", "Python 3.11+"),
        (b"ATEABLE_UNSUPPORTED_OS PRIVATE_SECRET", "Linux VMs only"),
        (b"remote port forwarding failed PRIVATE_SECRET", "unused tunnel port"),
        (b"ATEABLE_PAIR_FAILED PRIVATE_SECRET", "fresh pairing key"),
    ],
)
def test_ssh_errors_are_actionable_without_echoing_secrets(
    capture_popen, diagnostic, expected
):
    processes, _calls = capture_popen
    processes.append(FakeProcess(stderr=diagnostic, returncode=255))
    manager = SshProvisioner()
    with pytest.raises(SshDeviceError) as error:
        manager._run(
            SshDeviceSpec("vm"),
            "true",
            b"secret-key",
            stage="Provisioning failed",
            timeout=1,
        )
    assert expected in str(error.value)
    assert "PRIVATE_SECRET" not in str(error.value)
    assert "secret-key" not in str(error.value)


def test_pair_key_and_proxy_credentials_only_cross_stdin(capture_popen, key):
    processes, calls = capture_popen
    process = ready_process()
    processes.append(process)
    proxy = "http://user:private-password@proxy.example:8080"
    spec = SshDeviceSpec(
        "vm", network_mode="proxy", proxy=proxy, output_dir="/srv/files $(literal)"
    )
    manager = SshProvisioner()
    handle = manager.connect(spec, "http://127.0.0.1:8765", key, timeout=1)
    arguments, _options = calls[0]
    assert key not in " ".join(arguments)
    assert proxy not in " ".join(arguments)
    assert "private-password" not in repr(spec)
    payload = json.loads(process.stdin.getvalue())
    assert payload["pair_key"] == key
    assert payload["proxy"] == proxy
    assert payload["output_dir"] == spec.output_dir
    assert payload["coordinator_url"] == f"http://127.0.0.1:{spec.reverse_port}"
    assert handle.worker_id == "worker-one"
    assert handle.poll() is None
    manager.close()
    assert handle.poll() == 0


def test_s3_settings_cross_stdin_as_nonsecret_metadata(capture_popen, key):
    processes, calls = capture_popen
    process = ready_process()
    processes.append(process)
    spec = SshDeviceSpec(
        "vm",
        s3_endpoint="https://objects.example/s3",
        s3_profile="worker-downloads",
    )
    manager = SshProvisioner()
    try:
        manager.connect(spec, "http://127.0.0.1:8765", key, timeout=1)
        payload = json.loads(process.stdin.getvalue())
        assert payload["s3_endpoint"] == spec.s3_endpoint
        assert payload["s3_profile"] == spec.s3_profile
        assert spec.s3_endpoint not in " ".join(calls[0][0])
        assert spec.s3_profile not in " ".join(calls[0][0])
        assert (
            not {"aws_access_key_id", "aws_secret_access_key", "aws_session_token"}
            & payload.keys()
        )
    finally:
        manager.close()


@pytest.mark.parametrize("configured", [True, False])
def test_worker_script_sets_s3_selection_and_keeps_vm_credentials(
    monkeypatch, tmp_path, configured
):
    observed = {}

    class StubWorker:
        def __init__(self, config):
            self.config = config

        def run(self):
            observed.update(
                {
                    name: os.getenv(name)
                    for name in (
                        "ATEABLE_S3_ENDPOINT",
                        "ATEABLE_S3_PROFILE",
                        "AWS_PROFILE",
                        "AWS_ACCESS_KEY_ID",
                        "AWS_SECRET_ACCESS_KEY",
                    )
                }
            )

    monkeypatch.setitem(
        sys.modules,
        "ateable.remote_worker",
        SimpleNamespace(
            RemoteWorker=StubWorker,
            WorkerConfig=lambda **values: SimpleNamespace(**values),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "ateable.network",
        SimpleNamespace(DEFAULT_TOR_PROXY="socks5h://127.0.0.1:9050"),
    )
    settings = {
        "remote_dir": str(tmp_path),
        "coordinator_url": "http://127.0.0.1:28765",
        "name": "vm",
        "concurrency": 1,
        "network_mode": "direct",
        "s3_endpoint": "http://minio.internal:9000" if configured else None,
        "s3_profile": "download-profile" if configured else None,
    }
    monkeypatch.setattr(
        sys,
        "stdin",
        SimpleNamespace(buffer=io.BytesIO(json.dumps(settings).encode() + b"\n")),
    )
    monkeypatch.setenv("PATH", os.getenv("PATH", ""))
    for name, value in {
        "ATEABLE_S3_ENDPOINT": "https://vm-original.example",
        "ATEABLE_S3_PROFILE": "vm-profile",
        "AWS_PROFILE": "vm-profile",
        "AWS_ACCESS_KEY_ID": "vm-only-access",
        "AWS_SECRET_ACCESS_KEY": "vm-only-secret",
    }.items():
        monkeypatch.setenv(name, value)
    previous_umask = os.umask(0o077)
    try:
        exec(compile(ssh_devices._WORKER_SCRIPT, "worker-test", "exec"), {})  # noqa: S102 -- exercise our packaged worker script with stubbed imports
    finally:
        os.umask(previous_umask)
    assert observed["ATEABLE_S3_ENDPOINT"] == (
        settings["s3_endpoint"] if configured else "https://vm-original.example"
    )
    assert observed["ATEABLE_S3_PROFILE"] == (
        settings["s3_profile"] if configured else "vm-profile"
    )
    assert observed["AWS_PROFILE"] == (
        settings["s3_profile"] if configured else "vm-profile"
    )
    assert observed["AWS_ACCESS_KEY_ID"] == "vm-only-access"
    assert observed["AWS_SECRET_ACCESS_KEY"] == "vm-only-secret"


def test_store_round_trips_nonsecret_s3_preset_without_access_keys(tmp_path):
    path = tmp_path / "ssh-devices.json"
    store = SshDeviceStore(path)
    spec = SshDeviceSpec(
        "vm", s3_endpoint="http://minio.internal:9000", s3_profile="downloads"
    )
    store.save("My VM", spec)
    assert store.load()["My VM"] == spec
    record = json.loads(path.read_text())["devices"]["My VM"]
    assert record["s3_endpoint"] == spec.s3_endpoint
    assert record["s3_profile"] == spec.s3_profile
    assert (
        not {"aws_access_key_id", "aws_secret_access_key", "aws_session_token"}
        & record.keys()
    )


def test_existing_ssh_presets_without_s3_fields_still_load(tmp_path):
    path = tmp_path / "ssh-devices.json"
    path.write_text(json.dumps({"version": 1, "devices": {"Old VM": {"host": "vm"}}}))
    spec = SshDeviceStore(path).load()["Old VM"]
    assert spec.s3_endpoint is None
    assert spec.s3_profile is None


def test_duplicate_connection_reuses_managed_handle(capture_popen, key):
    processes, calls = capture_popen
    processes.append(ready_process())
    manager = SshProvisioner()
    spec = SshDeviceSpec("vm")
    first = manager.connect(spec, "http://127.0.0.1:8765", key, timeout=1)
    assert manager.connect(spec, "http://127.0.0.1:8765", key, timeout=1) is first
    assert len(calls) == 1
    with pytest.raises(SshDeviceError, match="already connected"):
        manager.connect(
            SshDeviceSpec("vm", concurrency=2), "http://127.0.0.1:8765", key
        )
    manager.close()


def test_restart_uses_remote_credentials_without_a_new_key(capture_popen, key):
    processes, calls = capture_popen
    old_process = ready_process("same-worker")
    new_process = ready_process("same-worker")
    processes.extend([old_process, new_process])
    manager = SshProvisioner()
    spec = SshDeviceSpec("vm")
    first = manager.connect(spec, "http://localhost:8765", key, timeout=1)
    old_process.returncode = 255
    second = manager.connect(spec, "http://localhost:8765", timeout=1)
    assert first is not second
    assert second.worker_id == first.worker_id
    assert json.loads(new_process.stdin.getvalue())["pair_key"] is None
    assert len(calls) == 2
    manager.stop_all()


def test_registration_without_verified_heartbeat_is_not_online(capture_popen, key):
    processes, _calls = capture_popen
    process = ready_process(heartbeat=False)
    processes.append(process)
    manager = SshProvisioner()
    with pytest.raises(SshDeviceError, match="handshake timed out"):
        manager.connect(SshDeviceSpec("vm"), "http://127.0.0.1:8765", key, timeout=0.01)
    assert not manager._handles
    assert process.stdin.closed


@pytest.mark.parametrize(
    "controller",
    [
        "http://192.168.1.4:8765",
        "https://localhost:8765",
        "http://admin:secret@localhost:8765",
        "http://localhost:8765/subpath",
    ],
)
def test_controller_tunnel_only_targets_local_loopback(capture_popen, key, controller):
    _processes, calls = capture_popen
    with pytest.raises(ValueError, match="local HTTP"):
        SshProvisioner().connect(SshDeviceSpec("vm"), controller, key)
    assert not calls


def test_close_prevents_late_background_launch(capture_popen):
    processes, calls = capture_popen
    process = FakeProcess()
    processes.append(process)
    manager = SshProvisioner()
    manager._start(["ssh", "vm"])
    manager.close()
    manager.close()
    assert process.terminated
    with pytest.raises(SshDeviceError, match="closed"):
        manager.connect(SshDeviceSpec("vm"), "http://127.0.0.1:8765")
    with pytest.raises(SshDeviceError, match="closed"):
        manager.provision(SshDeviceSpec("vm"))
    assert len(calls) == 1


@pytest.fixture
def windows_file_capture(monkeypatch):
    monkeypatch.setattr(ssh_devices, "_is_windows", lambda: True)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    calls = []

    def install(*, output=b"done\n", errors=b"", returncode=0, timeout=False):
        class FileProcess(FakeProcess):
            def communicate(self, input_data=None, timeout=None):
                self.input_data = input_data
                if self.timed_out and not self.killed:
                    raise subprocess.TimeoutExpired("ssh", timeout)
                self.output_file.write(output)
                self.errors_file.write(errors)
                self.returncode = returncode
                return None, None

        def popen(arguments, **kwargs):
            process = FileProcess()
            process.timed_out = timeout
            process.output_file = kwargs["stdout"]
            process.errors_file = kwargs["stderr"]
            assert process.output_file != subprocess.PIPE
            assert process.errors_file != subprocess.PIPE
            assert kwargs["stdin"] == subprocess.PIPE
            assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
            calls.append((process, arguments, kwargs))
            return process

        monkeypatch.setattr(ssh_devices.subprocess, "Popen", popen)
        return calls

    return install


def test_windows_one_shot_ssh_captures_files_and_keeps_payload_on_stdin(
    windows_file_capture,
):
    calls = windows_file_capture()
    manager = SshProvisioner()
    result = manager._run(
        SshDeviceSpec("vm"), "sh -s", b"private-source", stage="Install", timeout=3
    )
    process, arguments, _kwargs = calls[0]
    assert result == b"done\n"
    assert process.input_data == b"private-source"
    assert "private-source" not in " ".join(arguments)
    assert process.output_file.closed and process.errors_file.closed
    assert not manager._pending


def test_windows_one_shot_timeout_kills_pending_process_and_closes_capture(
    windows_file_capture,
):
    calls = windows_file_capture(timeout=True)
    manager = SshProvisioner()
    with pytest.raises(SshDeviceError, match="timed out"):
        manager._run(SshDeviceSpec("vm"), "true", b"", stage="Install", timeout=0.01)
    process = calls[0][0]
    assert process.killed
    assert process.output_file.closed and process.errors_file.closed
    assert not manager._pending


def test_windows_stdin_timeout_stops_before_expensive_provisioning(
    windows_file_capture, monkeypatch
):
    calls = windows_file_capture(timeout=True)
    manager = SshProvisioner()
    monkeypatch.setattr(
        manager, "source_archive", lambda: pytest.fail("No archive after stdin timeout")
    )
    with pytest.raises(SshDeviceError, match="Git for Windows"):
        manager.provision(SshDeviceSpec("vm"))
    assert len(calls) == 1 and calls[0][0].killed
    assert calls[0][0].output_file.closed and calls[0][0].errors_file.closed
    assert not manager._pending


def test_ssh_io_failure_kills_reaps_and_untracks_owned_child(
    capture_popen, monkeypatch
):
    processes, _calls = capture_popen
    process = FakeProcess()
    processes.append(process)
    reaped = []

    def communicate(*args, **kwargs):
        raise OSError("PRIVATE_SECRET")

    def wait(timeout=None):
        reaped.append(timeout)
        return process.returncode

    monkeypatch.setattr(process, "communicate", communicate)
    monkeypatch.setattr(process, "wait", wait)
    manager = SshProvisioner()
    with pytest.raises(SshDeviceError) as error:
        manager._run(
            SshDeviceSpec("vm"), "sh -s", b"private-source", stage="Install", timeout=1
        )
    assert process.killed and reaped == [5]
    assert all(
        stream.closed for stream in (process.stdin, process.stdout, process.stderr)
    )
    assert not manager._pending
    assert "PRIVATE_SECRET" not in str(error.value)
    assert "private-source" not in str(error.value)


def test_windows_one_shot_output_limit_and_sanitized_error_tail(
    windows_file_capture, monkeypatch
):
    monkeypatch.setattr(ssh_devices, "_MAX_SSH_OUTPUT_BYTES", 64)
    calls = windows_file_capture(output=b"x" * 65)
    manager = SshProvisioner()
    with pytest.raises(SshDeviceError, match="response size limit"):
        manager._run(SshDeviceSpec("vm"), "true", b"", stage="Install", timeout=1)
    assert calls[0][0].output_file.closed
    calls = windows_file_capture(
        errors=b"x" * 128 + b"Permission denied (publickey). PRIVATE_SECRET",
        returncode=255,
    )
    with pytest.raises(SshDeviceError, match="password prompts are disabled") as error:
        manager._run(SshDeviceSpec("vm"), "true", b"", stage="Install", timeout=1)
    assert "PRIVATE_SECRET" not in str(error.value)
    assert calls[0][0].errors_file.closed


def test_windows_close_cancels_owned_one_shot_process(
    windows_file_capture, monkeypatch
):
    calls = windows_file_capture()
    manager = SshProvisioner()
    started, cancelled = threading.Event(), threading.Event()
    outcomes = []
    factory = ssh_devices.subprocess.Popen

    def popen(*args, **kwargs):
        process = factory(*args, **kwargs)

        def communicate(input_data=None, timeout=None):
            process.input_data = input_data
            started.set()
            assert cancelled.wait(2)
            return None, None

        def terminate():
            process.terminated = True
            process.returncode = -15
            cancelled.set()

        process.communicate = communicate
        process.terminate = terminate
        return process

    monkeypatch.setattr(ssh_devices.subprocess, "Popen", popen)

    def run():
        try:
            manager._run(SshDeviceSpec("vm"), "true", b"", stage="Install", timeout=3)
        except SshDeviceError as error:
            outcomes.append(str(error))

    task = threading.Thread(target=run)
    task.start()
    assert started.wait(2)
    manager.close()
    task.join(2)
    assert not task.is_alive()
    assert outcomes and "closed" in outcomes[0]
    assert calls[0][0].terminated
    assert calls[0][0].output_file.closed and calls[0][0].errors_file.closed
    assert not manager._pending


def test_store_remembers_paths_without_worker_secrets(tmp_path):
    target = tmp_path / "devices.json"
    store = SshDeviceStore(target)
    spec = SshDeviceSpec(
        "config-alias", identity_file="/keys/worker.pem", output_dir="/srv/downloads"
    )
    store.save("download VM", spec)
    assert store.load() == {"download VM": spec}
    text = target.read_text()
    assert "pair_key" not in text and "worker_token" not in text
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600
    store.remove("download VM")
    assert store.load() == {}


def test_store_refuses_plaintext_proxy_credentials(tmp_path):
    store = SshDeviceStore(tmp_path / "devices.json")
    spec = SshDeviceSpec(
        "vm", proxy="http://private:secret@proxy.example:3128", network_mode="proxy"
    )
    with pytest.raises(ValueError, match="credentials cannot be saved"):
        store.save("VM", spec)
    assert not store.path.exists()


def test_remote_scripts_compile_and_remote_installer_is_shell_valid():
    for script in (
        ssh_devices._DEPLOY_SOURCE,
        ssh_devices._LAUNCH_WORKER,
        ssh_devices._WORKER_SCRIPT,
    ):
        compile(script, "remote-bootstrap", "exec")
    if os.name != "nt":
        result = subprocess.run(
            ["sh", "-n"],
            input=ssh_devices._INSTALL_SYSTEM.encode(),
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr.decode()


@pytest.fixture
def system_installer_shell(tmp_path):
    """Run the real bootstrap with a PATH containing only inert test commands."""
    if os.name == "nt":
        pytest.skip("POSIX remote bootstrap shell")
    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("POSIX shell is unavailable")
    commands = tmp_path / "commands"
    commands.mkdir()
    log = tmp_path / "calls"
    ready = tmp_path / "python-ready"

    def command(name, source):
        target = commands / name
        target.write_text("#!/bin/sh\nset -eu\n" + source, encoding="utf-8")
        target.chmod(0o700)

    command("uname", "printf 'Linux\\n'\n")
    command("id", "printf '%s\\n' \"$ATEABLE_TEST_UID\"\n")
    command("python3", '[ -f "$ATEABLE_TEST_READY" ]\n')
    command("ffmpeg", "exit 0\n")
    command(
        "env",
        '[ "$1" = DEBIAN_FRONTEND=noninteractive ]\nshift\nexec "$@"\n',
    )

    def run(
        manager=None, *, prepared=False, tor=False, user=False, sudo=True, denied=False
    ):
        if prepared:
            ready.touch()
        if manager:
            command(
                manager,
                f"printf '{manager} %s\\n' " + '"$*" >> "$ATEABLE_TEST_LOG"\n'
                ': > "$ATEABLE_TEST_READY"\n',
            )
        if sudo:
            command(
                "sudo",
                'printf \'sudo %s\\n\' "$*" >> "$ATEABLE_TEST_LOG"\n'
                '[ "$1" = -n ]\nshift\n'
                'if [ "$ATEABLE_TEST_SUDO_FAIL" = 1 ]; then\n'
                '    printf "sudo: a password is required\\n" >&2; exit 1\n'
                'fi\nexec "$@"\n',
            )
        result = subprocess.run(
            [shell],
            input=ssh_devices._INSTALL_SYSTEM,
            check=False,
            text=True,
            capture_output=True,
            env={
                "PATH": str(commands),
                "ATEABLE_TEST_LOG": str(log),
                "ATEABLE_TEST_READY": str(ready),
                "ATEABLE_TEST_UID": "1000" if user else "0",
                "ATEABLE_TEST_SUDO_FAIL": "1" if denied else "0",
                "ATEABLE_REQUIRE_TOR": "1" if tor else "0",
            },
            timeout=5,
        )
        return result, log.read_text().splitlines() if log.exists() else []

    return run


@pytest.mark.parametrize(
    ("manager", "expected"),
    [
        (
            "apt-get",
            [
                "apt-get update -qq",
                "apt-get install -y -qq python3 python3-venv python3-pip ffmpeg",
            ],
        ),
        ("dnf", ["dnf install -y python3 python3-pip ffmpeg"]),
        ("apk", ["apk add --no-cache python3 py3-pip ffmpeg"]),
        (
            "pacman",
            ["pacman --sync --needed --noconfirm python python-pip ffmpeg"],
        ),
    ],
)
def test_system_installer_executes_supported_package_manager_safely(
    system_installer_shell, manager, expected
):
    result, calls = system_installer_shell(manager)
    assert result.returncode == 0, result.stderr
    assert calls == expected


def test_arch_system_installer_adds_tor_and_uses_noninteractive_sudo(
    system_installer_shell,
):
    result, calls = system_installer_shell("pacman", prepared=True, tor=True, user=True)
    assert result.returncode == 0, result.stderr
    install = "pacman --sync --needed --noconfirm python python-pip ffmpeg tor"
    assert calls == ["sudo -n " + install, install]


def test_prepared_worker_skips_system_install_without_sudo(system_installer_shell):
    result, calls = system_installer_shell(prepared=True, user=True, sudo=False)
    assert result.returncode == 0, result.stderr
    assert calls == []


def test_system_installer_fails_without_privileges(system_installer_shell):
    result, calls = system_installer_shell("pacman", user=True, sudo=False)
    assert result.returncode == 62
    assert "ATEABLE_NO_PRIVILEGES" in result.stderr
    assert calls == []


def test_system_installer_never_prompts_for_sudo_password(system_installer_shell):
    result, calls = system_installer_shell("pacman", user=True, denied=True)
    assert result.returncode != 0
    assert "a password is required" in result.stderr
    assert len(calls) == 1 and calls[0].startswith("sudo -n pacman ")


@pytest.mark.skipif(os.name == "nt", reason="Linux supervisor process-group contract")
def test_remote_supervisor_stops_worker_when_controller_stdin_closes(tmp_path):
    """Exercise the real remote lifetime code without SSH or network access."""
    runtime = tmp_path / "venv" / "bin"
    runtime.mkdir(parents=True)
    (runtime / "python").symlink_to(sys.executable)
    worker_script = "import json,os,sys,time; json.loads(sys.stdin.buffer.readline()); print(os.getpid(),flush=True); time.sleep(60)"
    script = ssh_devices._LAUNCH_WORKER.replace(
        repr(ssh_devices._WORKER_SCRIPT), repr(worker_script)
    )
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    pid_result = []
    reader = threading.Thread(
        target=lambda: pid_result.append(process.stdout.readline()), daemon=True
    )
    try:
        process.stdin.write(json.dumps({"remote_dir": str(tmp_path)}).encode() + b"\n")
        process.stdin.flush()
        reader.start()
        reader.join(timeout=5)
        assert not reader.is_alive(), "Worker failed to start"
        worker_pid = int(pid_result[0])
        process.stdin.close()
        process.wait(timeout=5)
        with pytest.raises(ProcessLookupError):
            os.kill(worker_pid, 0)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()
