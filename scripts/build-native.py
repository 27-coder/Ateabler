#!/usr/bin/env python3
"""Build a native Ateable GUI and CLI pack on the current operating system.

Use a fresh Python 3.12 environment with backend[desktop] and
packaging/native/build-requirements.txt. Worker payload dependencies are rejected.
No installation, application state access, signing identity, or publishing occurs.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import tomllib
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

WORKER_DISTRIBUTIONS = {
    "boto3",
    "botocore",
    "s3transfer",
    "yt-dlp",
    "yt-dlp-ejs",
    "imageio-ffmpeg",
}
BUILD_DISTRIBUTIONS = {
    "pip",
    "setuptools",
    "wheel",
    "hatchling",
    "editables",
    "pathspec",
    "trove-classifiers",
    "pyinstaller",
    "pyinstaller-hooks-contrib",
    "altgraph",
    "macholib",
    "pefile",
    "pywin32-ctypes",
    "packaging",
}
ENTRY_NAMES = ("Ateable", "ateable-cli", "ateable-node", "ateable-node-ui")


def platform_target(
    system: str | None = None, machine: str | None = None
) -> tuple[str, str]:
    system = (system or platform.system()).lower()
    target = {"darwin": "macos", "linux": "linux", "windows": "windows"}.get(system)
    architecture = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get((machine or platform.machine()).lower())
    if target is None or architecture is None:
        raise ValueError(
            "Native packs support Linux, macOS, and Windows on x86_64 or arm64"
        )
    if target == "windows" and architecture != "x86_64":
        raise ValueError(
            "The pinned Windows Qt wheel supports x86_64; use the source installer on ARM64"
        )
    return target, architecture


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def copy_file(source: Path, destination: Path) -> None:
    if not source.is_file() or source.is_symlink():
        raise ValueError(f"Expected a regular source file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def prepare_resources(source: Path, destination: Path) -> list[tuple[str, str]]:
    """Allowlist source assets; never traverse arbitrary runtime/state folders."""
    for name in ("pyproject.toml", "LICENSE", "THIRD_PARTY_NOTICES.md"):
        copy_file(source / "backend" / name, destination / "worker-source" / name)
    modules = sorted((source / "backend" / "ateable").glob("*.py"))
    if not modules:
        raise ValueError("Ateable worker source is missing")
    for path in modules:
        copy_file(path, destination / "worker-source" / "ateable" / path.name)
    for location in ("ateable/assets", "worker-source/ateable/assets"):
        copy_file(
            source / "backend/ateable/assets/logo-white-on-black.png",
            destination / location / "logo-white-on-black.png",
        )
    for name in (
        "QUICKSTART.md",
        "PACKAGING.md",
        "README.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "CHANGELOG.md",
        "AUTOMATION.md",
        "ROADMAP.md",
        "PRODUCT.md",
        "LINUX-COMPATIBILITY.md",
        "docs/WINDOWS-SSH.md",
        "LICENSE",
        "packaging/install/INSTALL.md",
        "browser-extension/README.md",
    ):
        copy_file(source / name, destination / "docs" / name)
    # The module itself defines a fixed package asset list shared with browser ZIPs.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "browser_builder", source / "scripts" / "build-browser-extension.py"
    )
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    for name in (*builder.ASSETS, "manifest.json", "manifest.firefox.json"):
        copy_file(
            source / "browser-extension" / name,
            destination / "browser-extension" / name,
        )
    extensions = destination / "outputs" / "browser-extensions"
    extensions.mkdir(parents=True)
    for browser in ("chrome", "firefox"):
        content, version = builder.build(source / "browser-extension", browser)
        for suffix in ("zip", "xpi") if browser == "firefox" else ("zip",):
            (extensions / f"ateable-capture-{browser}-{version}.{suffix}").write_bytes(
                content
            )
    licenses = destination / "licenses"
    copy_file(source / "LICENSE", licenses / "LICENSE")
    copy_file(source / "THIRD_PARTY_NOTICES.md", licenses / "THIRD_PARTY_NOTICES.md")
    for path in sorted((source / "packaging/native/licenses").glob("*")):
        component = (
            "pyinstaller" if path.name.startswith("PyInstaller-") else "qt-6.8.3"
        )
        copy_file(path, licenses / component / path.name)
    if not (licenses / "qt-6.8.3/QtBase-6.8.3-NOTICES.txt").is_file():
        raise ValueError("Vendored Qt 6.8.3 license texts are missing")
    return [
        (str(path), path.name)
        for path in (
            destination / "worker-source",
            destination / "browser-extension",
            destination / "ateable",
            destination / "docs",
            licenses,
        )
    ] + [(str(extensions), "outputs/browser-extensions")]


def dependency_metadata(destination: Path) -> tuple[list[dict], list[dict]]:
    runtime, tools = [], []
    for dist in sorted(
        importlib.metadata.distributions(),
        key=lambda item: item.metadata["Name"].lower(),
    ):
        name = dist.metadata["Name"]
        normalized = name.lower().replace("_", "-")
        if normalized in WORKER_DISTRIBUTIONS:
            raise ValueError(
                f"Worker-only distribution is installed: {name}; use a fresh desktop build environment"
            )
        if (
            normalized in {"pyside6-essentials", "shiboken6"}
            and dist.version != "6.8.3"
        ):
            raise ValueError(
                "Desktop packs require Qt/PySide6 6.8.3 to match the included license notices"
            )
        if normalized == "pyinstaller" and dist.version != "6.22.3":
            raise ValueError(
                "Native packs require PyInstaller 6.22.3 to match the included bootloader license"
            )
        item = {
            "name": name,
            "version": dist.version,
            "license": dist.metadata.get("License-Expression")
            or dist.metadata.get("License")
            or "See included license files",
            "homepage": dist.metadata.get("Home-page")
            or dist.metadata.get("Project-URL", ""),
        }
        if normalized in BUILD_DISTRIBUTIONS:
            tools.append(item)
        else:
            runtime.append(item)
        for relative in dist.files or ():
            # Wheel-installed license texts, Qt notices, and Python package copyright.
            text = str(relative).replace("\\", "/")
            leaf = Path(text).name.lower()
            if not any(
                value in leaf
                for value in ("license", "licence", "copying", "copyright", "notice")
            ):
                continue
            located = Path(dist.locate_file(relative))
            if (
                not located.is_file()
                or located.is_symlink()
                or located.stat().st_size > 4 * 1024 * 1024
            ):
                continue
            # Exclude source code whose filename happens to contain 'license'.
            if located.suffix in {".py", ".pyc", ".so", ".dll", ".dylib"}:
                continue
            safe = text.replace("../", "").lstrip("/")
            copy_file(located, destination / normalized / safe)
    if not any(
        item["name"].lower().replace("_", "-") == "pyside6-essentials"
        for item in runtime
    ):
        raise ValueError("PySide6-Essentials is required for a desktop pack")
    (destination / "DEPENDENCIES.json").write_text(
        json.dumps(runtime, indent=2) + "\n", "utf-8"
    )
    (destination / "README.txt").write_text(
        "Ateable includes dynamically linked Qt 6 / PySide6 libraries. Their license texts and notices are included below.\n"
        "Those shared libraries remain separate from the application executable and can be replaced by compatible builds.\n"
        "Qt source and licensing information: https://www.qt.io/development/open-source-qt\n"
        "Python license: https://docs.python.org/3/license.html\n"
        "Exact installed package versions are recorded in DEPENDENCIES.json and build-manifest.json.\n",
        "utf-8",
    )
    # Python's standard library license is independent of Python wheel metadata.
    candidates = (
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sysconfig.get_path("stdlib")) / "LICENSE.txt",
        Path(sys.base_prefix)
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "LICENSE.txt",
    )
    python_license = next((path for path in candidates if path.is_file()), None)
    if python_license is None:
        raise ValueError(
            "The build interpreter's LICENSE.txt is missing; use an official Python distribution that includes its license"
        )
    copy_file(python_license, destination / "python" / "LICENSE.txt")
    return runtime, tools


def run(command: list[str], *, environment: dict[str, str] | None = None) -> None:
    subprocess.run(
        command,
        check=True,
        env=environment,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )


def pyinstaller_command(source: Path, work: Path, dist: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(dist),
        "--workpath",
        str(work / "pyinstaller"),
        str(source / "packaging" / "native" / "ateable.spec"),
    ]


def write_executable(path: Path, text: str) -> None:
    path.write_text(text, "utf-8")
    path.chmod(0o755)


def finish_layout(distribution: Path, package: Path, target: str, version: str) -> Path:
    package.mkdir(parents=True)
    if target != "macos":
        root = package / "Ateable"
        shutil.copytree(distribution / "Ateable", root, symlinks=True)
        if target == "linux":
            write_executable(
                package / "Ateable.desktop",
                '[Desktop Entry]\nType=Application\nName=Ateable\nComment=Control downloads on remote workers\nExec=sh -c \'"$(dirname "$1")/Ateable/Ateable"\' sh %k\nIcon=folder-download\nTerminal=false\nCategories=Network;\n',
            )
        return root
    app = package / "Ateable.app"
    shutil.copytree(distribution / "Ateable.app", app, symlinks=True)
    for name in ("ateable-cli", "ateable-node", "ateable-node-ui"):
        write_executable(
            package / name,
            f'#!/bin/sh\nset -eu\nbase=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\nexec "$base/Ateable.app/Contents/MacOS/{name}" "$@"\n',
        )
    # A lightweight second Finder launch target uses the same Qt/Python bundle.
    node = package / "Ateable Node.app" / "Contents"
    (node / "MacOS").mkdir(parents=True)
    write_executable(
        node / "MacOS" / "AteableNode",
        '#!/bin/sh\nset -eu\nbase=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)\nexec "$base/Ateable.app/Contents/MacOS/ateable-node-ui" "$@"\n',
    )
    (node / "Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": "org.ateable.node",
                "CFBundleExecutable": "AteableNode",
                "CFBundleName": "Ateable Node",
                "CFBundleDisplayName": "Ateable Node",
                "CFBundlePackageType": "APPL",
                "CFBundleShortVersionString": version,
                "LSMinimumSystemVersion": "12.0",
                "NSHighResolutionCapable": True,
            }
        )
    )
    return app / "Contents" / "MacOS"


def smoke(executable_dir: Path, target: str, runner: Callable = run) -> None:
    suffix = ".exe" if target == "windows" else ""
    with tempfile.TemporaryDirectory(prefix="ateable-native-smoke-") as temporary:
        environment = os.environ.copy()
        environment.update(
            {
                "ATEABLE_CONFIG_DIR": temporary,
                "ATEABLE_DATA_DIR": temporary,
                "QT_QPA_PLATFORM": "offscreen",
                "XDG_CONFIG_HOME": temporary,
                "XDG_DATA_HOME": temporary,
                "XDG_CACHE_HOME": temporary,
            }
        )
        for name in ("ateable-cli", "ateable-node"):
            runner(
                [str(executable_dir / (name + suffix)), "--help"],
                environment=environment,
            )
        for name in ("Ateable", "ateable-node-ui"):
            runner(
                [str(executable_dir / (name + suffix)), "--native-smoke"],
                environment=environment,
            )


def make_archive(package: Path, destination: Path, target: str) -> None:
    if target == "macos":
        # ditto preserves framework symbolic links and app executable permissions.
        run(
            [
                "ditto",
                "-c",
                "-k",
                "--sequesterRsrc",
                "--keepParent",
                str(package),
                str(destination),
            ]
        )
    elif target == "windows":
        with zipfile.ZipFile(
            destination, "w", zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            for path in sorted(package.rglob("*")):
                if path.is_file():
                    archive.write(
                        path, str(path.relative_to(package.parent)).replace("\\", "/")
                    )
    else:
        with tarfile.open(destination, "w:gz", compresslevel=6) as archive:
            archive.add(package, arcname=package.name)


def build(
    source: Path,
    output: Path,
    work: Path,
    *,
    dmg: bool = False,
    skip_smoke: bool = False,
) -> dict:
    target, architecture = platform_target()
    if dmg and target != "macos":
        raise ValueError("DMG output can only be built on macOS")
    if not (3, 11) <= sys.version_info[:2] <= (3, 13):
        raise ValueError(
            "Native build supports Python 3.11 through 3.13; release CI uses 3.12"
        )
    version = tomllib.loads((source / "backend" / "pyproject.toml").read_text("utf-8"))[
        "project"
    ]["version"]
    if any(
        character not in "0123456789.-abcdefghijklmnopqrstuvwxyz"
        for character in version
    ):
        raise ValueError("Unsafe project version")
    identifier = f"ateable-native-{version}-{target}-{architecture}"
    output.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    resources = work / "resources"
    distribution = work / "dist"
    package = work / identifier
    for path in (resources, distribution, package):
        if path.exists():
            raise ValueError(
                f"Build destination already exists; select a new --work-dir: {path}"
            )
    resources.mkdir()
    datas = prepare_resources(source, resources)
    dependencies, build_tools = dependency_metadata(resources / "licenses")
    manifest = {
        "format": 1,
        "name": "Ateable",
        "version": version,
        "platform": target,
        "architecture": architecture,
        "python": platform.python_version(),
        "build_os": platform.platform(),
        "libc": list(platform.libc_ver()),
        "built_at": datetime.now(UTC).isoformat(),
        "signed": False,
        "notarized": False,
        "worker_payload_dependencies_bundled": False,
        "entrypoints": list(ENTRY_NAMES),
        "dependencies": dependencies,
        "build_tools": build_tools,
        "sources": {
            str(path.relative_to(resources)).replace("\\", "/"): digest(path)
            for path in sorted(resources.rglob("*"))
            if path.is_file()
        },
    }
    manifest_path = resources / "build-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", "utf-8")
    datas.append((str(manifest_path), "."))
    inputs = work / "build-inputs.json"
    inputs.write_text(
        json.dumps(
            {
                "source": str(source),
                "version": version,
                "architecture": architecture,
                "datas": datas,
            }
        ),
        "utf-8",
    )
    environment = os.environ.copy()
    environment["ATEABLE_NATIVE_INPUTS"] = str(inputs)
    environment["PYINSTALLER_CONFIG_DIR"] = str(work / "pyinstaller-cache")
    run(pyinstaller_command(source, work, distribution), environment=environment)
    binaries = finish_layout(distribution, package, target, version)
    for name in ENTRY_NAMES:
        if not (binaries / (name + (".exe" if target == "windows" else ""))).is_file():
            raise ValueError(f"Native executable is missing: {name}")
    copy_file(manifest_path, package / "build-manifest.json")
    shutil.copytree(resources / "docs", package / "docs")
    shutil.copytree(resources / "licenses", package / "licenses")
    readme = (
        f"Ateable {version} — {target} {architecture}\n\n"
        "Admin GUI: Ateable.exe (Windows), Ateable/Ateable (Linux), or Ateable.app (macOS).\n"
        "Node GUI: ateable-node-ui.exe / ateable-node-ui, or Ateable Node.app on macOS.\n"
        "Console commands: ateable-cli --help and ateable-node --help (Windows: add .exe).\n"
        "Windows/Linux commands live inside the Ateable folder; macOS wrappers sit beside the apps.\n"
        "On macOS keep Ateable.app and Ateable Node.app together in the extracted folder.\n"
        "No Python installation is required. SSH worker provisioning still requires the system ssh command.\n"
        "Start with docs/QUICKSTART.md, or the Start tab in Admin.\n"
        f"Built on {platform.platform()}, Python {platform.python_version()}, libc {' '.join(platform.libc_ver()) or 'not applicable'}.\n"
        "Linux requires glibc at least as new as the build host; use the source installer on older distributions.\n"
        "Qt 6.8 targets macOS 12+ and Windows 10 version 1809+; exact versions are in the build manifest.\n"
        "This is an unsigned local build. State is created in your user folders when you launch an app.\n"
        "Downloaded payloads are stored on paired workers; the admin package contains control code and metadata.\n"
        "Firefox and Chrome/Edge capture assets are inside the bundled browser-extension folder.\n"
        "Licenses and exact dependency versions are in licenses/ and build-manifest.json.\n"
    )
    (package / "README.txt").write_text(readme, "utf-8")
    if not skip_smoke:
        smoke(binaries, target)
    extension = ".tar.gz" if target == "linux" else ".zip"
    archive = output / (identifier + extension)
    if archive.exists():
        raise ValueError(f"Artifact already exists: {archive}")
    make_archive(package, archive, target)
    artifacts = [archive]
    if dmg:
        image = output / (identifier + ".dmg")
        if image.exists():
            raise ValueError(f"Artifact already exists: {image}")
        run(
            [
                "hdiutil",
                "create",
                "-volname",
                f"Ateable {version}",
                "-srcfolder",
                str(package),
                "-format",
                "UDZO",
                "-ov",
                str(image),
            ]
        )
        artifacts.append(image)
    (output / (identifier + ".sha256")).write_text(
        "".join(f"{digest(path)}  {path.name}\n" for path in artifacts), "utf-8"
    )
    copy_file(manifest_path, output / (identifier + ".build.json"))
    return {
        "platform": target,
        "architecture": architecture,
        "version": version,
        "artifacts": [
            {"path": str(path), "sha256": digest(path), "bytes": path.stat().st_size}
            for path in artifacts
        ],
        "smoke_tested": not skip_smoke,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, default=Path("build/native"))
    parser.add_argument(
        "--dmg", action="store_true", help="Also create a macOS disk image"
    )
    parser.add_argument(
        "--skip-smoke",
        action="store_true",
        help="Build without executing bundled apps (not for releases)",
    )
    arguments = parser.parse_args()
    try:
        result = build(
            arguments.source_dir.resolve(),
            arguments.output_dir.resolve(),
            arguments.work_dir.resolve(),
            dmg=arguments.dmg,
            skip_smoke=arguments.skip_smoke,
        )
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Native build failed: {exc}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
