"""Bounded storage metadata collected on a worker, never on the controller."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class WorkerInventoryTracker:
    def __init__(self, output_dir: Path, state_file: Path):
        self.output_dir = output_dir.resolve()
        self.state_file = state_file
        self._lock = threading.RLock()
        try:
            rows = json.loads(state_file.read_text(encoding="utf-8"))
            self._files = {row["job_id"]: row for row in rows if isinstance(row, dict)}
        except (OSError, ValueError, TypeError, KeyError):
            self._files: dict[str, dict[str, Any]] = {}

    def record(self, job_id: str, path: str) -> int:
        output = Path(path).resolve()
        if not output.is_relative_to(self.output_dir):
            raise ValueError("Completed file is outside the worker output directory")
        size = output.stat().st_size
        with self._lock:
            self._files[job_id] = {"job_id": job_id, "path": str(output)}
            # The durable controller ledger contains all completed jobs; only
            # recent presence checks are needed in the worker's heartbeat.
            self._files = dict(list(self._files.items())[-200:])
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_file.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(list(self._files.values())), encoding="utf-8"
            )
            temporary.chmod(0o600)
            temporary.replace(self.state_file)
        return size

    def snapshot(self) -> dict[str, Any]:
        usage = shutil.disk_usage(self.output_dir)
        size, count = 0, 0
        deadline = time.monotonic() + 2
        scan_complete = True
        # Read file metadata only. Do not follow symlinks outside the output root.
        for parent, directories, names in os.walk(self.output_dir, followlinks=False):
            directories[:] = [
                name for name in directories if not Path(parent, name).is_symlink()
            ]
            for name in names:
                if time.monotonic() >= deadline:
                    scan_complete = False
                    break
                if name.endswith((".part", ".ytdl", ".tmp")):
                    continue
                path = Path(parent, name)
                try:
                    if path.is_file() and not path.is_symlink():
                        size += path.stat().st_size
                        count += 1
                except OSError:
                    continue
            if not scan_complete:
                break
        with self._lock:
            files = []
            for row in self._files.values():
                path = Path(row["path"])
                try:
                    safe = path.resolve().is_relative_to(self.output_dir)
                    present = safe and path.is_file()
                    file_size = path.stat().st_size if present else None
                except OSError:
                    present, file_size = False, None
                files.append(
                    {
                        "job_id": row["job_id"],
                        "exists": present,
                        "size_bytes": file_size,
                    }
                )
        return {
            "inventory": {
                "output_dir": str(self.output_dir),
                "disk_total_bytes": usage.total,
                "disk_free_bytes": usage.free,
                "downloaded_bytes": size,
                "file_count": count,
                "sampled_at": datetime.now(UTC).isoformat(),
                "scan_complete": scan_complete,
            },
            "files": files,
        }
