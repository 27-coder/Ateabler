#!/usr/bin/env python3
"""Build deterministic source install packs without third-party build tools."""

from __future__ import annotations

import argparse
import base64
import csv
import gzip
import hashlib
import importlib.util
import io
import json
import re
import tarfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EPOCH = 315532800
PLATFORMS = ("linux", "windows", "macos")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def zip_bytes(files: dict[str, bytes], executable: set[str] = frozenset()) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (0o100755 if name in executable else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data, compresslevel=9)
    return output.getvalue()


def source_wheel(root: Path, *, node_only: bool = False) -> tuple[str, bytes, str]:
    project = tomllib.loads((root / "backend/pyproject.toml").read_text())["project"]
    version = project["version"]
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*(?:[a-zA-Z0-9.]+)?", version):
        raise ValueError("Project version must be safe for wheel and pack filenames")
    if node_only:
        project = {
            **project,
            "name": "ateable-node-client",
            "description": "Scoped Ateable workspace participant client",
            "dependencies": [],
            "optional-dependencies": {
                "desktop": project["optional-dependencies"]["desktop"]
            },
            "scripts": {"ateable-node": "ateable.participant:main"},
        }
    name = project["name"].replace("-", "_")
    dist = f"{name}-{version}.dist-info"
    files: dict[str, bytes] = {}
    source = root / "backend/ateable"
    for path in sorted(source.rglob("*.py")):
        if node_only and path.relative_to(source).as_posix() not in {
            "__init__.py",
            "participant.py",
            "participant_ui.py",
            "branding.py",
        }:
            continue
        if path.is_symlink() or "__pycache__" in path.parts:
            raise ValueError(f"Unsafe source entry: {path}")
        files[path.relative_to(root / "backend").as_posix()] = path.read_bytes()
    logo = source / "assets/logo-white-on-black.png"
    if logo.is_symlink() or not logo.is_file():
        raise ValueError("Approved logo asset is missing")
    files["ateable/assets/logo-white-on-black.png"] = logo.read_bytes()
    if "ateable/__init__.py" not in files:
        raise ValueError("Ateable package is missing")
    if not node_only:
        for source_name in ("pyproject.toml", "LICENSE", "THIRD_PARTY_NOTICES.md"):
            files[source_name] = (root / "backend" / source_name).read_bytes()
    metadata = [
        "Metadata-Version: 2.4",
        f"Name: {project['name']}",
        f"Version: {version}",
        f"Summary: {project['description']}",
        f"Requires-Python: {project['requires-python']}",
        f"License-Expression: {project['license']}",
    ]
    metadata += [f"Requires-Dist: {item}" for item in project["dependencies"]]
    for extra, requirements in sorted(project["optional-dependencies"].items()):
        if extra == "test":
            continue
        metadata.append(f"Provides-Extra: {extra}")
        for requirement in requirements:
            if ";" in requirement:
                value, marker = requirement.split(";", 1)
                metadata.append(
                    f'Requires-Dist: {value.strip()}; ({marker.strip()}) and extra == "{extra}"'
                )
            else:
                metadata.append(f'Requires-Dist: {requirement}; extra == "{extra}"')
    for license_name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        files[f"{dist}/licenses/{license_name}"] = (
            root / "backend" / license_name
        ).read_bytes()
        metadata.append(f"License-File: {license_name}")
    files[f"{dist}/METADATA"] = ("\n".join(metadata) + "\n\n").encode()
    files[f"{dist}/WHEEL"] = (
        b"Wheel-Version: 1.0\nGenerator: Ateable source pack builder\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )
    entries = ["[console_scripts]"] + [
        f"{key} = {value}" for key, value in sorted(project["scripts"].items())
    ]
    files[f"{dist}/entry_points.txt"] = ("\n".join(entries) + "\n").encode()
    records = io.StringIO(newline="")
    writer = csv.writer(records, lineterminator="\n")
    for filename, data in sorted(files.items()):
        hashed = (
            base64.urlsafe_b64encode(hashlib.sha256(data).digest())
            .rstrip(b"=")
            .decode()
        )
        writer.writerow((filename, f"sha256={hashed}", len(data)))
    writer.writerow((f"{dist}/RECORD", "", ""))
    files[f"{dist}/RECORD"] = records.getvalue().encode()
    return f"{name}-{version}-py3-none-any.whl", zip_bytes(files), version


def build(
    root: Path, output: Path, platforms: tuple[str, ...] = PLATFORMS
) -> list[Path]:
    wheel_name, wheel, version = source_wheel(root)
    node_wheel_name, node_wheel, _ = source_wheel(root, node_only=True)
    spec = importlib.util.spec_from_file_location(
        "ateable_extension_builder", root / "scripts/build-browser-extension.py"
    )
    if spec is None or spec.loader is None:
        raise ValueError("Browser extension builder is unavailable")
    extensions = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(extensions)
    output.mkdir(parents=True, exist_ok=True)
    artifacts = []
    for platform in platforms:
        pack = f"ateable-install-{version}-{platform}"
        files = {
            "install.py": (root / "packaging/install/install.py").read_bytes(),
            "launch.py": (root / "packaging/install/launch.py").read_bytes(),
            "INSTALL.md": (root / "packaging/install/INSTALL.md").read_bytes(),
            "QUICKSTART.md": (root / "QUICKSTART.md").read_bytes(),
            "LICENSE": (root / "backend/LICENSE").read_bytes(),
            "THIRD_PARTY_NOTICES.md": (
                root / "backend/THIRD_PARTY_NOTICES.md"
            ).read_bytes(),
            f"wheels/{wheel_name}": wheel,
            f"wheels/{node_wheel_name}": node_wheel,
        }
        for name in (
            "PACKAGING.md",
            "AUTOMATION.md",
            "README.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "CHANGELOG.md",
            "ROADMAP.md",
            "PRODUCT.md",
            "LINUX-COMPATIBILITY.md",
            "docs/WINDOWS-SSH.md",
            "packaging/install/INSTALL.md",
            "browser-extension/README.md",
        ):
            files[name] = (root / name).read_bytes()
        for asset in (*extensions.ASSETS, "manifest.json", "manifest.firefox.json"):
            files[f"resources/browser-extension/{asset}"] = (
                root / "browser-extension" / asset
            ).read_bytes()
        files["resources/scripts/capture-to-bucket.py"] = (
            root / "scripts/capture-to-bucket.py"
        ).read_bytes()
        for browser in ("chrome", "firefox"):
            content, extension_version = extensions.build(
                root / "browser-extension", browser
            )
            for suffix in ("zip", "xpi") if browser == "firefox" else ("zip",):
                files[
                    f"resources/outputs/browser-extensions/ateable-capture-{browser}-{extension_version}.{suffix}"
                ] = content
        executable = {"install.py"}
        if platform == "windows":
            files["install.ps1"] = (root / "packaging/install/install.ps1").read_bytes()
        else:
            entry = "install.command" if platform == "macos" else "install.sh"
            files[entry] = (root / "packaging/install/install.sh").read_bytes()
            executable.add(entry)
        manifest = {
            "format": 1,
            "platform": platform,
            "version": version,
            "wheel": f"wheels/{wheel_name}",
            "node_wheel": f"wheels/{node_wheel_name}",
            "files": {name: digest(data) for name, data in sorted(files.items())},
        }
        files["pack.json"] = (
            json.dumps(manifest, sort_keys=True, indent=2) + "\n"
        ).encode()
        files["SHA256SUMS"] = "".join(
            f"{digest(data)}  {name}\n" for name, data in sorted(files.items())
        ).encode()
        if platform == "windows":
            destination = output / f"{pack}.zip"
            destination.write_bytes(
                zip_bytes(
                    {f"{pack}/{name}": data for name, data in files.items()},
                    {f"{pack}/{name}" for name in executable},
                )
            )
        else:
            destination = output / f"{pack}.tar.gz"
            with (
                destination.open("wb") as stream,
                gzip.GzipFile(
                    fileobj=stream, mode="wb", mtime=EPOCH, filename="", compresslevel=9
                ) as compressed,
                tarfile.open(
                    fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT
                ) as archive,
            ):
                for name, data in sorted(files.items()):
                    info = tarfile.TarInfo(f"{pack}/{name}")
                    info.size, info.mtime = len(data), EPOCH
                    info.mode = 0o755 if name in executable else 0o644
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    archive.addfile(info, io.BytesIO(data))
        artifacts.append(destination)
    (output / "INSTALL-SHA256SUMS").write_text(
        "".join(f"{digest(path.read_bytes())}  {path.name}\n" for path in artifacts)
    )
    return artifacts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "release/install")
    parser.add_argument("--platform", choices=("all", *PLATFORMS), default="all")
    args = parser.parse_args()
    for path in build(
        ROOT, args.output_dir, PLATFORMS if args.platform == "all" else (args.platform,)
    ):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
