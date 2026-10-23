"""Install a narrow participant launcher beside a VM's private worker state."""

import os
import shlex
import tempfile
from pathlib import Path

HEADER = "#!/bin/sh\n# Ateable Node launcher\n"


def install_node_launcher(base: Path) -> Path:
    root = base.expanduser().resolve()
    directory = root / "bin"
    target = directory / "ateable-node"
    if directory.is_symlink() or target.is_symlink():
        raise ValueError("Node launcher directory must not be a symlink")
    if target.exists() and not target.read_text().startswith(HEADER):
        raise ValueError("An unrelated Node launcher already occupies this path")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    python = root / "venv" / "bin" / "python"
    credentials = root / "worker-credentials.json"
    content = (
        HEADER
        + "ATEABLE_WORKER_CREDENTIALS="
        + shlex.quote(str(credentials))
        + "\nexport ATEABLE_WORKER_CREDENTIALS\nexec "
        + shlex.quote(str(python))
        + " -m ateable.participant"
        + ' "$@"\n'
    )
    descriptor, temporary = tempfile.mkstemp(prefix=".ateable-node-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
        Path(temporary).chmod(0o700)
        Path(temporary).replace(target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target
