"""Opt-in native build check; uses no settings, controller, or network."""

from __future__ import annotations

import importlib.util
import os
import sys
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


class MemorySettings:
    """Keep the actual Admin window away from the user's Qt preferences."""

    def __init__(self, *_args, **_kwargs):
        self.values: dict[str, object] = {}

    def value(self, key, default=None):
        return self.values.get(key, default)

    def setValue(self, key, value):
        self.values[key] = value

    def remove(self, key):
        self.values.pop(key, None)

    def sync(self):
        return None


def maybe_smoke() -> None:
    if sys.argv[1:] != ["--native-smoke"]:
        return
    try:
        code = _smoke()
    except Exception as exc:  # noqa: BLE001 - smoke-only errors must not open bootloader dialogs
        # These two entry points use a windowed bootloader. An ordinary unhandled
        # exception would show its error dialog during unattended build checks.
        if sys.stderr is not None:
            print(
                f"Native UI smoke failed: {type(exc).__name__}: {exc}", file=sys.stderr
            )
        code = 1
    raise SystemExit(code)


def _smoke() -> int:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    root = Path(sys._MEIPASS)
    for required in (
        "worker-source/pyproject.toml",
        "worker-source/ateable/remote_worker.py",
        "browser-extension/manifest.firefox.json",
        "build-manifest.json",
        "licenses/THIRD_PARTY_NOTICES.md",
        "ateable/assets/logo-white-on-black.png",
        "docs/QUICKSTART.md",
    ):
        if not (root / required).is_file():
            raise SystemExit(2)
    for excluded in ("boto3", "botocore", "yt_dlp", "imageio_ffmpeg"):
        if importlib.util.find_spec(excluded) is not None:
            raise SystemExit(3)
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QLabel

    app = QApplication.instance() or QApplication(["Ateable native build check"])
    from ateable.branding import app_icon, logo_pixmap

    if logo_pixmap(64).isNull() or app_icon().isNull():
        raise SystemExit(4)

    with (
        TemporaryDirectory(prefix="ateable-native-smoke-") as temporary,
        ExitStack() as stack,
    ):
        scratch = Path(temporary)
        stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "ATEABLE_CONFIG_DIR": str(scratch / "config"),
                    "ATEABLE_STATE_DIR": str(scratch / "state"),
                    "ATEABLE_DATABASE_PATH": str(scratch / "unused.db"),
                    "ATEABLE_OUTPUT_DIR": str(scratch / "unused-output"),
                    "ATEABLE_WORKER_CREDENTIALS": str(scratch / "unused-worker.json"),
                    "ATEABLE_PARTICIPANT_SESSION": str(scratch / "unused-session.json"),
                },
            )
        )
        stack.enter_context(
            patch(
                "socket.create_connection",
                side_effect=RuntimeError(
                    "Networking is disabled in the native UI smoke check"
                ),
            )
        )
        # The real window construction and layout run, while all controller and
        # participant operations remain disabled for this opt-in check.
        if Path(sys.executable).stem.lower() == "ateable-node-ui":
            from ateable.participant import ParticipantClient, ParticipantIdentity
            from ateable.participant_ui import ParticipantWindow

            identity = ParticipantIdentity(
                "https://controller.example.test",
                "native-smoke-agent",
                "agent",
                "native-smoke-private-placeholder",
                "native-smoke-workspace",
            )
            stack.enter_context(
                patch.object(ParticipantWindow, "_run_async", return_value=None)
            )
            window = ParticipantWindow(ParticipantClient(identity), auto_refresh=False)
            if window.tabs.count() != 4:
                raise RuntimeError("Node workspace tabs are missing")
        else:
            from ateable import desktop

            device = desktop.DeviceConfig(
                device_id="00000000-0000-0000-0000-000000000001",
                name="Native smoke check",
                role="controller",
                controller_url="https://controller.example.test",
                output_dir=str(scratch / "unused-output"),
                network_mode="direct",
                admin_key="native-smoke-private-placeholder",
            )
            stack.enter_context(patch.object(desktop, "QSettings", MemorySettings))
            stack.enter_context(
                patch.object(desktop.DeviceConfig, "load", return_value=device)
            )
            stack.enter_context(
                patch.object(desktop.DeviceConfig, "save", return_value=None)
            )
            stack.enter_context(
                patch.object(desktop.AteableWindow, "refresh_all", return_value=None)
            )
            stack.enter_context(
                patch.object(desktop.AteableWindow, "_run_async", return_value=None)
            )
            window = desktop.AteableWindow()
            window.poll_timer.stop()
            if (
                window.tabs.count() != 6
                or not hasattr(window, "start_tab")
                or window.tabs.currentWidget() is not window.start_tab
                or window.controller_runtime is not None
            ):
                raise RuntimeError("Admin startup guide or controller isolation failed")

        logos = [
            label
            for label in window.findChildren(QLabel)
            if label.accessibleName() == "Ateable logo"
        ]
        if not logos or any(label.pixmap().isNull() for label in logos):
            raise RuntimeError("The actual window is missing its branded logo")
        window.show()
        close_errors: list[Exception] = []

        def finish() -> None:
            try:
                window.close()
            except Exception as exc:  # noqa: BLE001 - preserve errors across the Qt callback boundary
                close_errors.append(exc)
            finally:
                app.quit()

        QTimer.singleShot(40, finish)
        code = app.exec()
        app.processEvents()
        if close_errors:
            raise RuntimeError("Native window did not close cleanly") from close_errors[
                0
            ]
        if (scratch / "unused.db").exists() or (scratch / "unused-output").exists():
            raise RuntimeError(
                "UI smoke unexpectedly created controller or download state"
            )
        return code
