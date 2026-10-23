"""Metadata-only desktop capture, including when the controller UI is closed."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import AppConfig, default_state_dir
from .database import ConflictError, Database, NotFoundError
from .validation import ValidationError, normalize_download_url

MAX_CAPTURE_BYTES = 5 * 1024 * 1024
EXTENSIONS = (".url", ".txt", ".csv", ".m3u", ".m3u8")
VERB = "Ateable.CaptureBucket"
URL = re.compile(r"(?<![\w:])(?:https?|s3)://[^\s<>\"']+", re.IGNORECASE)


def parse_capture_text(text: str, *, csv_mode: bool = False) -> list[dict[str, str]]:
    if len(text.encode("utf-8")) > MAX_CAPTURE_BYTES:
        raise ValueError("A capture list may contain at most 5 MiB of text")
    candidates: list[str] = []
    if csv_mode:
        for row in csv.reader(io.StringIO(text)):
            candidates.extend(
                value.strip()
                for value in row
                if value.strip().lower().startswith(("http://", "https://", "s3://"))
            )
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Whole URL lines preserve signed punctuation; prose uses URL matching.
            if not any(
                character.isspace() for character in line
            ) and line.lower().startswith(("http://", "https://", "s3://")):
                candidates.append(line)
            else:
                candidates.extend(URL.findall(line))
    items = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            canonical, key = normalize_download_url(candidate)
        except ValidationError:
            continue
        if len(canonical) > 8192:
            raise ValueError("A captured URL exceeds 8,192 characters")
        if key not in seen:
            seen.add(key)
            items.append({"url": canonical})
            if len(items) > 5000:
                raise ValueError("Capture at most 5,000 distinct links at a time")
    if not items:
        raise ValueError("No supported HTTP(S) or S3 links were found")
    return items


def read_capture_file(path: Path) -> list[dict[str, str]]:
    if path.suffix.lower() not in EXTENSIONS or not path.is_file():
        raise ValueError(
            "Choose an Internet shortcut or a URL list, not a local media file"
        )
    with path.open("rb") as stream:
        data = stream.read(MAX_CAPTURE_BYTES + 1)
    if len(data) > MAX_CAPTURE_BYTES:
        raise ValueError("Capture files are limited to 5 MiB")
    try:
        text = data.decode(
            "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        )
    except UnicodeError as exc:
        raise ValueError("Use a UTF-8 or BOM-marked UTF-16 URL list") from exc
    if "\x00" in text:
        raise ValueError("Binary files cannot be captured as URL lists")
    if path.suffix.lower() == ".url":
        found = next(
            (
                line.split("=", 1)[1].strip()
                for line in text.splitlines()
                if line.strip().lower().startswith("url=")
            ),
            None,
        )
        if not found:
            raise ValueError("The Internet shortcut has no URL")
        return parse_capture_text(found)
    return parse_capture_text(text, csv_mode=path.suffix.lower() == ".csv")


def local_bucket_store(state_dir: Path, database_path: Path | None = None):
    from .buckets import BucketStore

    state = state_dir.expanduser().resolve()
    database = Database(
        AppConfig(
            database_path=database_path or state / "ateable.db",
            output_dir=state / "unused-master-output",
            embedded_worker=False,
        )
    )
    database.initialize()
    return BucketStore(database)


def capture_local(
    state_dir: Path,
    items: list[dict[str, str]],
    bucket_id: str | None = None,
    database_path: Path | None = None,
) -> dict[str, Any]:
    store = local_bucket_store(state_dir, database_path)
    if bucket_id is None:
        bucket_id = "inbox"
    return store.capture(bucket_id, items, origin="pc")


def install_windows_menu(
    project_dir: Path,
    *,
    bucket_id: str | None = None,
    state_dir: Path | None = None,
    database_path: Path | None = None,
    registry: Any = None,
) -> list[str]:
    """Install only our own per-user verbs; preserve default file associations."""
    if registry is None:
        if os.name != "nt":
            raise RuntimeError("PC right-click setup is available on Windows")
        import winreg as registry
    root = project_dir.resolve()
    if getattr(sys, "frozen", False):
        # A packaged GUI executable handles capture before creating Qt. The
        # registry must point at the persistent install, never _MEIPASS.
        executable = Path(sys.executable).resolve().parent / "Ateable.exe"
        if not executable.is_file():
            raise ValueError("The installed Ateable capture executable is missing")
        launcher = [str(executable), "--capture-to-bucket"]
    else:
        executable = root / ".venv" / "Scripts" / "pythonw.exe"
        if not executable.is_file():
            active_pythonw = Path(sys.executable).resolve().with_name("pythonw.exe")
            if not active_pythonw.is_file():
                raise ValueError(
                    "Prepare the Ateable controller environment before installing capture"
                )
            executable = active_pythonw
        # Invoke a known script directly; Explorer's working directory varies.
        script = root / "scripts" / "capture-to-bucket.py"
        if not script.is_file():
            raise ValueError("The PC capture launcher is missing")
        launcher = [str(executable), str(script)]
    arguments = [
        *launcher,
        "--state-dir",
        str((state_dir or root / "data").resolve()),
        "--file",
        "%1",
        "--quiet",
    ]
    if bucket_id:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", bucket_id):
            raise ValueError("Invalid capture bucket identifier")
        arguments.extend(["--bucket-id", bucket_id])
    if database_path:
        arguments.extend(["--database-path", str(database_path.resolve())])
    # Explorer substitutes the filename after parsing this template. Quote the
    # placeholder even though list2cmdline sees no whitespace in the literal %1.
    command = subprocess.list2cmdline(arguments).replace(" %1 ", ' "%1" ')
    paths = []
    for extension in EXTENSIONS:
        path = rf"Software\Classes\SystemFileAssociations\{extension}\shell\{VERB}"
        with registry.CreateKeyEx(
            registry.HKEY_CURRENT_USER, path, 0, registry.KEY_WRITE
        ) as key:
            registry.SetValueEx(key, "", 0, registry.REG_SZ, "Ate it on bucket")
            registry.SetValueEx(key, "MultiSelectModel", 0, registry.REG_SZ, "Document")
            registry.SetValueEx(key, "Icon", 0, registry.REG_SZ, str(executable))
        with registry.CreateKeyEx(
            registry.HKEY_CURRENT_USER, path + r"\command", 0, registry.KEY_WRITE
        ) as key:
            registry.SetValueEx(key, "", 0, registry.REG_SZ, command)
        paths.append(path)
    return paths


def remove_windows_menu(registry: Any = None) -> None:
    if registry is None:
        if os.name != "nt":
            raise RuntimeError("PC right-click setup is available on Windows")
        import winreg as registry
    for extension in EXTENSIONS:
        path = rf"Software\Classes\SystemFileAssociations\{extension}\shell\{VERB}"
        for child in (path + r"\command", path):
            try:
                registry.DeleteKey(registry.HKEY_CURRENT_USER, child)
            except FileNotFoundError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture link metadata into an Ateable bucket without downloading it"
    )
    parser.add_argument("urls", nargs="*")
    parser.add_argument("--file", type=Path)
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    parser.add_argument("--bucket-id")
    parser.add_argument(
        "--database-path", type=Path, default=os.getenv("ATEABLE_DATABASE_PATH")
    )
    parser.add_argument("--install-menu", type=Path)
    parser.add_argument("--remove-menu", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.install_menu:
            result = {
                "installed": len(
                    install_windows_menu(
                        args.install_menu,
                        bucket_id=args.bucket_id,
                        state_dir=args.state_dir,
                        database_path=args.database_path,
                    )
                )
            }
        elif args.remove_menu:
            remove_windows_menu()
            result = {"removed": True}
        else:
            items = (
                read_capture_file(args.file)
                if args.file
                else parse_capture_text("\n".join(args.urls))
            )
            captured = capture_local(
                args.state_dir, items, args.bucket_id, args.database_path
            )
            result = {
                key: captured[key] for key in ("bucket_id", "accepted", "duplicates")
            }
        if not args.quiet and sys.stdout is not None:
            print(json.dumps(result))
        return 0
    except (
        ValueError,
        OSError,
        RuntimeError,
        sqlite3.Error,
        NotFoundError,
        ConflictError,
    ) as exc:
        if not args.quiet and sys.stderr is not None:
            print(str(exc), file=sys.stderr)
        # Explorer launches silently. Preserve only an error type, not URLs or keys.
        if args.quiet and args.file:
            args.state_dir.mkdir(parents=True, exist_ok=True)
            (args.state_dir / "capture-last-error.json").write_text(
                json.dumps(
                    {
                        "error": type(exc).__name__,
                        "message": "Capture did not succeed; inspect the URL list and chosen bucket in Ateable.",
                    }
                ),
                encoding="utf-8",
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
