#!/usr/bin/env python3
"""Install the selected Ateable role into a new, private user directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import venv
from pathlib import Path, PurePosixPath

PACK = Path(__file__).resolve().parent
SYSTEMS = {"linux": "linux", "darwin": "macos", "win32": "windows"}


def no_symlinks(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise ValueError(f"Symbolic link paths are not allowed: {candidate}")


def manifest(pack: Path = PACK) -> dict:
    path = pack / "pack.json"
    if path.is_symlink():
        raise ValueError("Pack manifest must not be a symbolic link")
    metadata = json.loads(path.read_text(encoding="utf-8"))
    if metadata.get("format") != 1 or metadata.get("platform") not in SYSTEMS.values():
        raise ValueError("Unsupported Ateable pack manifest")
    for relative, expected in metadata["files"].items():
        name = PurePosixPath(relative)
        if name.is_absolute() or ".." in name.parts or "\\" in relative:
            raise ValueError("Pack contains an unsafe file path")
        source = pack.joinpath(*name.parts)
        no_symlinks(source)
        if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Pack checksum does not match: {relative}")
    for field in ("wheel", "node_wheel"):
        if metadata[field] not in metadata["files"] or not metadata[field].endswith(
            ".whl"
        ):
            raise ValueError("Pack wheel is missing from its checksum manifest")
    return metadata


def default_prefix(role: str, version: str) -> Path:
    if sys.platform == "win32":
        base = (
            Path(os.getenv("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
            / "Ateable/installs"
        )
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support/Ateable/installs"
    else:
        base = (
            Path(os.getenv("XDG_DATA_HOME", str(Path.home() / ".local/share")))
            / "Ateable/installs"
        )
    return base / f"{role}-{version}"


def write(path: Path, content: str | bytes, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("xb") as stream:
        stream.write(content.encode() if isinstance(content, str) else content)
    path.chmod(0o700 if executable else 0o600)


def shell_launcher(prefix: Path, action: str, *, gui: bool = False) -> str:
    python = prefix / "venv/bin/python"
    return (
        "#!/bin/sh\nset -eu\nexec "
        + shlex.quote(str(python))
        + " "
        + shlex.quote(str(prefix / "launch.py"))
        + " "
        + shlex.quote(action)
        + ' "$@"\n'
    )


def windows_launcher(action: str, *, gui: bool = False) -> str:
    python = "pythonw.exe" if gui else "python.exe"
    # Relative paths keep %, &, parentheses and spaces in user paths out of command text.
    command = f'"%~dp0..\\venv\\Scripts\\{python}" "%~dp0..\\launch.py" {action} %*'
    if gui:
        command = 'start "" ' + command
    return f"@echo off\r\nsetlocal\r\n{command}\r\nexit /b %errorlevel%\r\n"


def private_pip(environment: Path, system: str) -> tuple[Path, list[str]]:
    """Create a private venv, using only locally available pip implementations."""
    venv.EnvBuilder(with_pip=False, symlinks=False).create(environment)
    python = environment / (
        "Scripts/python.exe" if system == "windows" else "bin/python"
    )
    bootstrap = subprocess.run(
        [str(python), "-m", "ensurepip", "--upgrade", "--default-pip"],
        check=False,
        capture_output=True,
        text=True,
    )
    if bootstrap.returncode == 0:
        return python, [str(python), "-m", "pip", "--isolated"]
    # pip 22.3+ can manage a venv that has no pip. This stays on the same target
    # computer and installs into the new venv; it never modifies the host Python.
    existing = [sys.executable, "-m", "pip", "--isolated", "--python", str(python)]
    probe = subprocess.run(
        [*existing, "--version"], check=False, capture_output=True, text=True
    )
    if probe.returncode != 0:
        raise ValueError(
            "Python needs working ensurepip, or pip 22.3+ in the interpreter running "
            "this installer. Use a complete Python or the matching native package; "
            "no system packages or bootstrap downloads were attempted."
        )
    print("Using existing target pip to manage the private environment.")
    return python, existing


def shortcuts(prefix: Path, role: str, gui: bool, system: str) -> list[Path]:
    actions = (
        [("ateable", "admin", False), ("Ateable-Desktop", "desktop", True)]
        if role == "admin"
        else [("ateable-node", "node", False)]
    )
    if role == "worker":
        actions.append(("ateable-worker", "worker", False))
    if role != "admin" and gui:
        actions.append(("Ateable-Node-Desktop", "node-ui", True))
    result = []
    for name, action, graphical in actions:
        target = prefix / "bin" / (name + ".cmd" if system == "windows" else name)
        content = (
            windows_launcher(action, gui=graphical)
            if system == "windows"
            else shell_launcher(prefix, action, gui=graphical)
        )
        write(target, content, executable=True)
        result.append(target)
        if graphical and system == "macos":
            app = prefix / f"{name}.app/Contents"
            write(app / "MacOS/launch", shell_launcher(prefix, action), executable=True)
            write(
                app / "Info.plist",
                plistlib.dumps(
                    {
                        "CFBundleExecutable": "launch",
                        "CFBundleName": name,
                        "CFBundleIdentifier": f"local.ateable.source.{role}",
                        "CFBundlePackageType": "APPL",
                        "NSHighResolutionCapable": True,
                    }
                ),
            )
        elif graphical and system == "linux":
            escaped = (
                str(target)
                .replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("`", "\\`")
                .replace("$", "\\$")
                .replace("%", "%%")
            )
            write(
                prefix / f"{name}.desktop",
                f'[Desktop Entry]\nType=Application\nName={name}\nExec="{escaped}"\nTerminal=false\nCategories=Network;\nIcon=folder-download\n',
            )
    return result


def install(args: argparse.Namespace, *, pack: Path = PACK) -> list[Path]:
    if sys.version_info < (3, 11):  # noqa: UP036 - installer checks the user's interpreter
        raise ValueError("Python 3.11 or newer is required")
    metadata = manifest(pack)
    system = SYSTEMS.get(sys.platform)
    if metadata["platform"] != system:
        raise ValueError(
            f"This {metadata['platform']} pack cannot install on {sys.platform}; choose the matching OS pack"
        )
    prefix = (
        (args.prefix or default_prefix(args.role, metadata["version"]))
        .expanduser()
        .absolute()
    )
    if any(ord(character) < 32 for character in str(prefix)):
        raise ValueError("Installation path must not contain control characters")
    no_symlinks(prefix)
    if prefix.exists():
        raise ValueError(
            f"Installation directory already exists; choose a new --prefix: {prefix}"
        )
    if args.offline and args.role != "node" and args.wheelhouse is None:
        raise ValueError(
            "Offline admin/worker installs require --wheelhouse with compatible dependency wheels"
        )
    if args.offline and args.gui and args.wheelhouse is None:
        raise ValueError(
            "Offline Node GUI installs require --wheelhouse with Qt dependency wheels"
        )
    if args.wheelhouse is not None:
        wheelhouse = args.wheelhouse.expanduser().absolute()
        no_symlinks(wheelhouse)
        if not wheelhouse.is_dir():
            raise ValueError("Wheelhouse must be an existing directory")
    wheel = pack / metadata["node_wheel" if args.role == "node" else "wheel"]
    extra = (
        "desktop"
        if args.role == "admin" or (args.role == "node" and args.gui)
        else "worker"
        if args.role == "worker"
        else ""
    )
    print(f"Role: {args.role}; OS: {system}; version: {metadata['version']}")
    print(f"New private installation: {prefix}")
    print(
        "Dependency network: disabled"
        if args.offline or (args.role == "node" and not args.gui)
        else "Dependency network: pip package index on this computer"
    )
    if args.dry_run:
        return []
    prefix.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    prefix.mkdir(mode=0o700)
    try:
        _, pip_command = private_pip(prefix / "venv", system)
        pip = [
            *pip_command,
            "install",
            "--disable-pip-version-check",
            "--no-input",
        ]
        if args.offline or (args.role == "node" and not args.gui):
            pip += ["--no-index"]
        if args.wheelhouse is not None:
            pip += ["--find-links", str(wheelhouse)]
        pip += [str(wheel) + (f"[{extra}]" if extra else "")]
        subprocess.run(pip, check=True)
        if args.role == "worker" and args.gui:
            qt = [
                *pip_command,
                "install",
                "--disable-pip-version-check",
                "--no-input",
            ]
            if args.offline:
                qt += ["--no-index"]
            if args.wheelhouse is not None:
                qt += ["--find-links", str(wheelhouse)]
            subprocess.run([*qt, "PySide6-Essentials>=6.8,<7"], check=True)
        # A worker receives only download service and participant launchers.
        if args.role == "worker":
            scripts = prefix / ("venv/Scripts" if system == "windows" else "venv/bin")
            for name in ("ateable", "Ateable", "ateable-api", "ateable-desktop"):
                for suffix in ("", ".exe", "-script.py", ".exe.manifest"):
                    candidate = scripts / (name + suffix)
                    if candidate.is_file():
                        candidate.unlink()
        write(prefix / "launch.py", (pack / "launch.py").read_bytes())
        write(
            prefix / "install.json",
            json.dumps(
                {
                    "format": 1,
                    "role": args.role,
                    "version": metadata["version"],
                    "gui": args.role == "admin" or args.gui,
                },
                indent=2,
            )
            + "\n",
        )
        for guide in (
            "INSTALL.md",
            "QUICKSTART.md",
            "PACKAGING.md",
            "AUTOMATION.md",
            "README.md",
            "ROADMAP.md",
            "PRODUCT.md",
            "LINUX-COMPATIBILITY.md",
            "docs/WINDOWS-SSH.md",
            "packaging/install/INSTALL.md",
            "browser-extension/README.md",
        ):
            write(prefix / guide, (pack / guide).read_bytes())
        if args.role == "admin":
            for relative in sorted(metadata["files"]):
                if relative.startswith("resources/"):
                    write(prefix / relative, (pack / relative).read_bytes())
        for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
            write(prefix / name, (pack / name).read_bytes())
        launchers = shortcuts(
            prefix, args.role, args.role == "admin" or args.gui, system
        )
        print("Installed. No GUI, server or worker has been started.")
        for launcher in launchers:
            print(f"Launcher: {launcher}")
        return launchers
    except BaseException:
        # Prefix was created exclusively by this invocation; existing state was never touched.
        shutil.rmtree(prefix)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("admin", "node", "worker"), default="admin")
    parser.add_argument("--prefix", type=Path)
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Add optional Node viewer (admin already includes desktop)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Never access a package index; use local wheelhouse for dependencies",
    )
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print plan without changing files or installing dependencies",
    )
    args = parser.parse_args()
    try:
        install(args)
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Installation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
