"""Private, role-limited source-install launchers; no automatic provisioning."""

from __future__ import annotations

import json
import os
import runpy
import sys
from pathlib import Path


def main() -> int:
    base = Path(__file__).resolve().parent
    marker = json.loads((base / "install.json").read_text(encoding="utf-8"))
    if len(sys.argv) < 2:
        raise SystemExit("Use one of this installation's launchers")
    action, args = sys.argv[1], sys.argv[2:]
    allowed = {
        "admin": {"desktop": "ateable.desktop", "admin": "ateable.cli"},
        "node": {"node": "ateable.participant"},
        "worker": {"node": "ateable.participant", "worker": "ateable.remote_worker"},
    }
    choices = allowed.get(marker["role"], {})
    if marker["role"] in {"node", "worker"} and marker.get("gui"):
        choices["node-ui"] = "ateable.participant_ui"
    if action not in choices:
        raise SystemExit("This launcher is unavailable for the installed role")
    state = base / "state"
    defaults = {
        "ATEABLE_STATE_DIR": str(state),
        "ATEABLE_DATABASE_PATH": str(state / "ateable.db"),
        "ATEABLE_CONFIG_DIR": str(state / "config"),
        "ATEABLE_OUTPUT_DIR": str(state / "downloads"),
        "ATEABLE_WORKER_CREDENTIALS": str(state / "worker-credentials.json"),
        "ATEABLE_PARTICIPANT_SESSION": str(state / "agent-session.json"),
    }
    if marker["role"] == "admin":
        defaults["ATEABLE_RESOURCE_ROOT"] = str(base / "resources")
    for key, value in defaults.items():
        os.environ.setdefault(key, value)
    os.environ["ATEABLE_EMBEDDED_WORKER"] = "false"
    sys.argv = [sys.argv[0], *args]
    if action == "node-ui":
        from ateable.participant_ui import main as ui_main

        return ui_main(args)
    runpy.run_module(choices[action], run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
