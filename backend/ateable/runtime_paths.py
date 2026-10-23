"""Resources shared by source, installer and frozen application layouts."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def resource_root() -> Path:
    configured = os.getenv("ATEABLE_RESOURCE_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))


def worker_source_directory(module_file: str | Path | None = None) -> Path:
    source = Path(module_file or __file__).resolve().parent.parent
    if (source / "pyproject.toml").is_file():
        return source
    return resource_root() / "worker-source"
