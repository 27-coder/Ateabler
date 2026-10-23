import json
import os
import shlex
import subprocess
import sys

import pytest

from ateable.node_launcher import install_node_launcher
from ateable.ssh_devices import SshDeviceSpec


@pytest.mark.skipif(os.name == "nt", reason="SSH node launchers target Unix workers")
def test_launcher_quotes_paths_and_preserves_cli_arguments(tmp_path):
    root = tmp_path / "worker's directory with spaces"
    python = root / "venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text(
        "#!/bin/sh\nexec "
        + shlex.quote(sys.executable)
        + ' -c \'import json,sys,os;print(json.dumps([sys.argv[1:],os.getenv("ATEABLE_WORKER_CREDENTIALS")]))\' "$@"\n'
    )
    python.chmod(0o700)
    launcher = install_node_launcher(root)
    output = subprocess.check_output(
        [str(launcher), "message", "send", "space", "hello with spaces"], text=True
    )
    arguments, credentials = json.loads(output)
    assert credentials == str(root / "worker-credentials.json")
    assert arguments == [
        "-m",
        "ateable.participant",
        "message",
        "send",
        "space",
        "hello with spaces",
    ]
    override = subprocess.check_output(
        [str(launcher), "--session-file", "agent-session.json", "identity"], text=True
    )
    assert json.loads(override)[0] == [
        "-m",
        "ateable.participant",
        "--session-file",
        "agent-session.json",
        "identity",
    ]
    assert install_node_launcher(root) == launcher
    if os.name != "nt":
        assert launcher.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(os.name == "nt", reason="SSH node launchers target Unix workers")
def test_launcher_preserves_unrelated_files_and_rejects_symlinks(tmp_path):
    directory = tmp_path / "bin"
    directory.mkdir()
    launcher = directory / "ateable-node"
    launcher.write_text("user file")
    with pytest.raises(ValueError, match="unrelated"):
        install_node_launcher(tmp_path)
    assert launcher.read_text() == "user file"
    launcher.unlink()
    launcher.symlink_to(tmp_path / "external")
    with pytest.raises(ValueError, match="symlink"):
        install_node_launcher(tmp_path)


def test_workspace_enrollment_does_not_change_remote_connection_identity():
    original = SshDeviceSpec("vm")
    enrolled = SshDeviceSpec("vm", workspace_id="workspace-1")
    assert original == enrolled
    assert original.connection_key == enrolled.connection_key
    with pytest.raises(ValueError, match="Workspace"):
        SshDeviceSpec("vm", workspace_id="../other")
