"""Installed app paths, windowless capture and launch behavior."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ateable import capture_pc, config, participant
from ateable.runtime_paths import resource_root, worker_source_directory


def test_installed_controller_uses_writable_user_data_without_creating_output(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delenv("ATEABLE_STATE_DIR", raising=False)
    monkeypatch.delenv("ATEABLE_DATABASE_PATH", raising=False)
    monkeypatch.setattr(config, "user_data_path", lambda *_, **__: tmp_path / "user")
    value = config.AppConfig.from_env()
    assert value.database_path == tmp_path / "user" / "data" / "ateable.db"
    assert value.embedded_worker is False
    assert not (tmp_path / "user").exists()
    monkeypatch.setenv("ATEABLE_STATE_DIR", str(tmp_path / "portable metadata"))
    assert config.default_state_dir() == tmp_path / "portable metadata"


def test_source_controller_keeps_its_project_data_default(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.delenv("ATEABLE_STATE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert config.default_state_dir() == tmp_path / "data"


def test_installer_resource_override_is_used_for_extensions_and_worker_source(
    tmp_path, monkeypatch
):
    desktop, _participant_ui = _qt_modules()
    from ateable import ssh_devices
    from ateable.buckets_ui import extension_directory

    resources = tmp_path / "installed resources"
    monkeypatch.setenv("ATEABLE_RESOURCE_ROOT", str(resources))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)
    missing_source = tmp_path / "installed" / "ateable" / "ssh_devices.py"
    monkeypatch.setattr(ssh_devices, "__file__", str(missing_source))
    assert resource_root() == resources
    assert extension_directory() == resources / "browser-extension"
    assert worker_source_directory(missing_source) == resources / "worker-source"
    assert ssh_devices.SshProvisioner().source_dir == resources / "worker-source"
    assert desktop.BACKEND_AVAILABLE


def test_installed_windows_capture_uses_active_venv_pythonw(tmp_path, monkeypatch):
    resources = tmp_path / "resources"
    (resources / "scripts").mkdir(parents=True)
    (resources / "scripts" / "capture-to-bucket.py").touch()
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "pythonw.exe").touch()
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "executable", str(scripts / "python.exe"))
    values = []

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    registry = SimpleNamespace(
        HKEY_CURRENT_USER="user",
        KEY_WRITE=1,
        REG_SZ=1,
        CreateKeyEx=lambda *_: Key(),
        SetValueEx=lambda _key, _name, _reserved, _kind, value: values.append(value),
    )
    capture_pc.install_windows_menu(resources, registry=registry)
    commands = [value for value in values if "--quiet" in value]
    assert len(commands) == 5
    assert str(scripts / "pythonw.exe") in commands[0]
    assert str(resources / "scripts" / "capture-to-bucket.py") in commands[0]


@pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
def test_node_uses_native_config_and_frozen_credential_paths(
    platform, tmp_path, monkeypatch
):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("ATEABLE_PARTICIPANT_SESSION", raising=False)
    monkeypatch.delenv("ATEABLE_WORKER_CREDENTIALS", raising=False)
    base = {
        "darwin": tmp_path / "Library" / "Application Support",
        "win32": tmp_path / "local",
        "linux": tmp_path / "config",
    }[platform]
    assert participant.default_session_file() == base / "ateable-node" / "session.json"
    assert (
        participant.default_credentials_file()
        == base / "Ateable" / "worker-credentials.json"
    )
    override = tmp_path / "vm-private" / "credentials.json"
    monkeypatch.setenv("ATEABLE_WORKER_CREDENTIALS", str(override))
    assert participant.default_credentials_file() == override


def test_frozen_explorer_capture_uses_persistent_gui_executable(tmp_path, monkeypatch):
    install = tmp_path / "Ateable install"
    install.mkdir()
    gui = install / "Ateable.exe"
    gui.touch()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(gui))
    values = []

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    registry = SimpleNamespace(
        HKEY_CURRENT_USER="user",
        KEY_WRITE=1,
        REG_SZ=1,
        CreateKeyEx=lambda *_: Key(),
        SetValueEx=lambda _key, _name, _reserved, _kind, value: values.append(value),
    )
    paths = capture_pc.install_windows_menu(
        tmp_path / "temporary bundle extraction",
        state_dir=tmp_path / "private metadata",
        database_path=tmp_path / "private metadata" / "db.sqlite",
        registry=registry,
    )
    assert len(paths) == 5
    commands = [value for value in values if "--capture-to-bucket" in value]
    assert len(commands) == 5
    for command in commands:
        assert str(gui) in command and '"%1"' in command
        assert "--quiet" in command and "--database-path" in command
        assert ".venv" not in command and "temporary bundle extraction" not in command


def test_admin_cli_imports_without_worker_payload_dependencies(tmp_path):
    source = Path(__file__).resolve().parents[1]
    script = """
import builtins
original = builtins.__import__
def filtered(name, *args, **kwargs):
    if name.split('.')[0] in {'yt_dlp', 'boto3', 'imageio_ffmpeg'}:
        raise ImportError('worker dependency deliberately absent')
    return original(name, *args, **kwargs)
builtins.__import__ = filtered
from ateable import cli, downloader, remote_worker, object_storage
try:
    cli.main(['--help'])
except SystemExit as exc:
    assert exc.code == 0
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        env={**os.environ, "PYTHONPATH": str(source)},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "usage: ateable" in result.stdout
    assert not list(tmp_path.iterdir())


def _qt_modules():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from ateable import desktop, participant_ui

    return desktop, participant_ui


def test_gui_help_and_capture_do_not_construct_application(tmp_path, monkeypatch):
    desktop, participant_ui = _qt_modules()
    never = Mock(side_effect=AssertionError("A help/capture action opened Qt"))
    monkeypatch.setattr(desktop, "QApplication", never)
    monkeypatch.setattr(participant_ui, "QApplication", never)
    assert desktop.main(["--help"]) == 0
    assert participant_ui.main(["--help"]) == 0
    listing = tmp_path / "urls.txt"
    listing.write_text("https://example.com/object.zip\n", encoding="utf-8")
    state = tmp_path / "metadata"
    assert (
        desktop.main(
            [
                "--capture-to-bucket",
                "--file",
                str(listing),
                "--state-dir",
                str(state),
                "--quiet",
            ]
        )
        == 0
    )
    assert capture_pc.local_bucket_store(state).list_items("inbox")["total"] == 1
    assert not (state / "unused-master-output").exists()
    never.assert_not_called()


def test_windowed_controller_disables_stream_probing(monkeypatch, tmp_path):
    desktop, _participant_ui = _qt_modules()
    app = config.AppConfig(tmp_path / "db", tmp_path / "unused")
    settings = []

    def make_config(_app, **kwargs):
        settings.append(kwargs)
        return kwargs

    server = Mock()
    monkeypatch.setattr(desktop, "create_controller_app", lambda _: object())
    monkeypatch.setattr(desktop.uvicorn, "Config", make_config)
    monkeypatch.setattr(desktop.uvicorn, "Server", lambda _: server)
    runtime = desktop.InProcessController(app)
    runtime._run()
    assert runtime.error is None
    assert settings[0]["log_config"] is None
    server.run.assert_called_once()


def test_node_gui_selects_bounded_local_identity_and_exposes_no_admin_controls(
    tmp_path, monkeypatch
):
    _desktop, ui = _qt_modules()
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    session = tmp_path / "agent.json"
    participant.save_session(
        session,
        {
            "controller_url": "https://controller.test",
            "agent_id": "analyst",
            "workspace_id": "workspace",
            "token": "SECRET-LOCAL-SESSION",
        },
    )
    monkeypatch.setattr(
        ui, "default_session_file", lambda: tmp_path / "missing-session"
    )
    monkeypatch.setattr(
        ui, "default_credentials_file", lambda: tmp_path / "missing-worker"
    )
    monkeypatch.setattr(ui, "_choose_access_file", lambda: session)
    opened = []
    monkeypatch.setattr(ui, "launch", lambda client: opened.append(client) or 0)
    assert ui.main([]) == 0
    assert opened[0].identity.actor_type == "agent"
    assert opened[0].identity.actor_id == "analyst"
    assert "X-Ateable-Key" not in opened[0].identity.headers()
    app.processEvents()


def test_node_gui_cancel_and_invalid_session_remain_readable(tmp_path, monkeypatch):
    _desktop, ui = _qt_modules()
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        ui, "default_session_file", lambda: tmp_path / "missing-session"
    )
    monkeypatch.setattr(
        ui, "default_credentials_file", lambda: tmp_path / "missing-worker"
    )
    monkeypatch.setattr(ui, "_choose_access_file", lambda: None)
    monkeypatch.setattr(ui, "launch", Mock(side_effect=AssertionError("Unexpected UI")))
    assert ui.main([]) == 0
    bad = tmp_path / "admin.json"
    bad.write_text(json.dumps({"admin_key": "DO-NOT-DISPLAY"}))
    errors = []
    monkeypatch.setattr(ui.QMessageBox, "critical", lambda *args: errors.append(args))
    assert ui.main(["--session-file", str(bad)]) == 1
    assert errors and "DO-NOT-DISPLAY" not in str(errors)
    assert ui.main(["--unexpected", "DO-NOT-DISPLAY"]) == 1
    assert "DO-NOT-DISPLAY" not in str(errors)
    app.processEvents()
