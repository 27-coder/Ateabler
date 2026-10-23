from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from ateable.cli import (
    ApiClient,
    CliError,
    allocate_urls,
    command_add,
    command_worker,
    main,
    normalize_args,
)
from ateable.device_config import DeviceConfig, default_config_path
from ateable.tor_manager import TorError, TorStatus, ensure_tor


def test_native_admin_refuses_worker_before_loading_credentials(monkeypatch):
    monkeypatch.setattr("ateable.cli.sys.frozen", True, raising=False)

    def forbidden_load():
        raise AssertionError("Native worker must be rejected before reading credentials")

    monkeypatch.setattr(DeviceConfig, "load", forbidden_load)
    with pytest.raises(CliError, match="source pack with --role worker"):
        command_worker(None)


@pytest.fixture
def config_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_dir = tmp_path / "config"
    monkeypatch.setenv("ATEABLE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("ATEABLE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATEABLE_DATABASE_PATH", str(tmp_path / "state" / "ateable.db"))
    monkeypatch.setenv("ATEABLE_OUTPUT_DIR", str(tmp_path / "downloads"))
    monkeypatch.setenv("ATEABLE_NETWORK_MODE", "direct")
    monkeypatch.delenv("ATEABLE_ADMIN_KEY", raising=False)
    return config_dir


def test_device_config_round_trip_is_private(
    config_environment: Path,
) -> None:
    config = DeviceConfig.defaults().updated(
        role="both",
        admin_key="admin-secret",
        worker_token="worker-secret",
    )
    path = config.save()
    loaded = DeviceConfig.load()
    assert loaded == config
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0


def test_setup_initializes_admin_and_device_info_alias(
    config_environment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["setup", "--role", "controller", "--network", "direct"]) == 0
    configured = DeviceConfig.load()
    assert configured.admin_key
    assert configured.last_pair_key
    capsys.readouterr()

    assert main(["DeviceInfo", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["role"] == "controller"
    assert result["device_id"] == configured.device_id
    assert result["pair_key"] == configured.last_pair_key
    assert result["admin_auth"]["ready"] is True
    assert result["admin_auth"]["key"] != configured.admin_key


def test_device_info_can_explicitly_show_admin_secret(
    config_environment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    DeviceConfig.defaults().updated(admin_key="show-this-secret").save()
    assert main(["DeviceInfo", "--show-secret", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["admin_auth"]["key"] == "show-this-secret"


def test_automation_setup_and_doctor_redact_device_identity(
    config_environment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                "--json",
                "setup",
                "--automation",
                "--role",
                "controller",
                "--network",
                "direct",
            ]
        )
        == 0
    )
    setup_result = json.loads(capsys.readouterr().out)
    assert setup_result["privacy"] == "redacted"
    assert setup_result["admin_auth_ready"] is True
    assert "device_id" not in setup_result
    assert "config_path" not in setup_result
    assert "output_dir" not in setup_result
    assert "controller_url" not in setup_result

    assert main(["--json", "doctor"]) == 0
    doctor_result = json.loads(capsys.readouterr().out)
    assert doctor_result["privacy"] == "redacted"
    serialized = json.dumps(doctor_result)
    configured = DeviceConfig.load()
    assert configured.device_id not in serialized
    assert configured.name not in serialized
    assert configured.output_dir not in serialized
    assert configured.last_pair_key not in serialized
    assert configured.admin_key not in serialized


def test_argument_aliases_are_case_insensitive() -> None:
    assert normalize_args(["DeviceInfo"]) == ["device", "info"]
    assert normalize_args(["DEVICE", "INFO"]) == ["device", "info"]
    assert normalize_args(["NetworkStatus", "--json"]) == [
        "--json",
        "network",
        "status",
    ]


def test_auto_allocation_is_deterministic_and_excludes_offline() -> None:
    workers = [
        {
            "id": "worker-b",
            "status": "online",
            "capabilities": ["wav"],
            "max_concurrency": 2,
        },
        {
            "id": "offline",
            "status": "offline",
            "capabilities": ["wav"],
            "max_concurrency": 50,
        },
        {
            "id": "local",
            "status": "online",
            "capabilities": ["wav"],
            "max_concurrency": 1,
        },
        {
            "id": "wrong-format",
            "status": "online",
            "capabilities": ["mp4"],
            "max_concurrency": 50,
        },
    ]
    urls = [f"url-{index}" for index in range(9)]
    allocation = allocate_urls(urls, workers, "wav")
    assert allocation == {"worker-b": urls}
    assert "local" not in allocation
    assert "offline" not in allocation


def test_add_auto_creates_one_batch_per_online_device(
    config_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    DeviceConfig.defaults().updated(admin_key="admin").save()
    requests: list[tuple[str, str, dict | None]] = []

    def fake_request(self, method, path, body=None, *, admin=True):
        requests.append((method, path, body))
        if path == "/api/workers":
            return {
                "items": [
                    {
                        "id": "local",
                        "status": "online",
                        "capabilities": ["wav"],
                        "max_concurrency": 1,
                    },
                    {
                        "id": "remote",
                        "status": "online",
                        "capabilities": ["wav"],
                        "max_concurrency": 1,
                    },
                ]
            }
        return {
            "batch": {"id": f"batch-{len(requests)}", "total": len(body["urls"])},
            "duplicates": [],
        }

    monkeypatch.setattr(ApiClient, "request", fake_request)
    args = type(
        "Args",
        (),
        {
            "urls": [f"https://example.com/file-{i}.zip" for i in range(4)],
            "file": None,
            "format": "wav",
            "device": "auto",
        },
    )()
    result = command_add(args)
    assert result["submitted"] == 4
    batch_bodies = [request[2] for request in requests if request[1] == "/api/batches"]
    assert [body["target_worker_id"] for body in batch_bodies] == ["remote"]
    assert batch_bodies[0]["urls"] == args.urls


def test_tor_mode_fails_closed_when_tor_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ateable.tor_manager.tor_status",
        lambda proxy, **_kwargs: TorStatus(
            managed=False,
            running=False,
            bootstrapped=False,
            proxy=proxy,
            executable=None,
            pid=None,
            torrc=None,
            data_dir=None,
            message="unreachable",
        ),
    )
    monkeypatch.setattr("ateable.tor_manager.find_tor", lambda: None)
    with pytest.raises(TorError, match="no tor/tor.exe"):
        ensure_tor("socks5h://127.0.0.1:9050", timeout=0.01)


def test_tor_bootstrap_honors_preexisting_cancellation() -> None:
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(TorError, match="cancelled"):
        ensure_tor(
            "socks5h://127.0.0.1:9050",
            timeout=0.01,
            cancel_event=cancelled,
        )


def test_default_config_path_honors_override(config_environment: Path) -> None:
    assert default_config_path() == config_environment / "config.json"


def test_default_add_uses_original_on_remote_workers(
    config_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    DeviceConfig.defaults().updated(admin_key="admin").save()
    submitted = []

    def request(self, method, path, body=None, **kwargs):
        if path == "/api/workers":
            return {
                "items": [
                    {"id": "remote", "status": "online", "capabilities": ["original"]}
                ]
            }
        submitted.append(body)
        return {"batch": {"id": "batch", "total": len(body["urls"])}, "duplicates": []}

    monkeypatch.setattr(ApiClient, "request", request)
    assert (
        main(["--json", "add", "https://example.com/archive.zip?signature=a,b%2B"]) == 0
    )
    assert submitted[0]["format"] == "original"
    assert submitted[0]["target_worker_id"] == "remote"
    assert submitted[0]["urls"] == ["https://example.com/archive.zip?signature=a,b%2B"]
    capsys.readouterr()
    assert (
        main(["--json", "add", "https://example.com/archive.zip", "--device", "local"])
        == 1
    )
    assert "cannot download" in capsys.readouterr().out
    assert len(submitted) == 1


def test_master_doctor_does_not_require_media_software(
    config_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    DeviceConfig.defaults().updated(admin_key="admin").save()

    def forbidden():
        raise AssertionError("master should not inspect download dependencies")

    monkeypatch.setattr("ateable.cli._readiness", forbidden)
    assert main(["--json", "doctor"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ready"] is True
    assert "ffmpeg" not in result["checks"]


def test_ssh_command_provisions_before_pairing_and_owns_tunnel_lifetime(
    config_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    DeviceConfig.defaults().updated(admin_key="admin").save()
    events = []

    class Handle:
        worker_id = "remote"

        def poll(self):
            return 0

    class Provisioner:
        def provision(self, spec):
            assert spec.host == "worker-alias"
            assert spec.network_mode == "direct"
            events.append("install")

        def connect(self, spec, controller_url, pair_key=None):
            assert pair_key == "fresh-invitation"
            events.append("connect")
            return Handle()

        def stop_all(self):
            events.append("stop")

    def request(self, method, path, body=None, **kwargs):
        assert path == "/api/pairing-keys"
        events.append("pair")
        return {"pair_key": "fresh-invitation"}

    monkeypatch.setattr("ateable.ssh_devices.SshProvisioner", Provisioner)
    monkeypatch.setattr(ApiClient, "request", request)
    assert main(["--json", "device", "ssh", "worker-alias"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["device_id"] == "remote"
    assert "keep this command running" in result["connection"]
    assert events == ["install", "pair", "connect", "stop"]
