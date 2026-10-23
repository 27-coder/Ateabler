"""Install missing media dependencies inside the worker's own directory."""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from .downloader import detect_js_runtimes

DENO_VERSION = "2.8.1"
RELEASE_API = (
    f"https://api.github.com/repos/denoland/deno/releases/tags/v{DENO_VERSION}"
)
# Node publishes official LinuxONE binaries; Deno does not publish s390x builds.
# Pin the release and its official SHASUMS256.txt value, verified 2026-10-19.
NODE_VERSION = "22.23.3"
NODE_RELEASE_URL = f"https://nodejs.org/download/release/v{NODE_VERSION}"
NODE_S390X_SHA256 = "51f64794f5f61bdfc80aa186c8273021473997605cf90ed31ed148bee650a554"
MAX_RUNTIME_SIZE = 250 * 1024 * 1024
MAX_RUNTIME_LICENSE_SIZE = 1024 * 1024


def _install_s390x_node(binary_dir: Path) -> Path:
    name = f"node-v{NODE_VERSION}-linux-s390x.tar.gz"
    checksums = _fetch(f"{NODE_RELEASE_URL}/SHASUMS256.txt", 128 * 1024)
    try:
        lines = checksums.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise RuntimeError(
            "ATEABLE_JS_RUNTIME: official runtime checksum is unavailable"
        ) from exc
    digests = []
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if match and match[2] == name:
            digests.append(match[1])
    if digests != [NODE_S390X_SHA256]:
        raise RuntimeError(
            "ATEABLE_JS_RUNTIME: official runtime checksum is unavailable or changed"
        )
    archive = _fetch(f"{NODE_RELEASE_URL}/{name}", 100 * 1024 * 1024)
    if hashlib.sha256(archive).hexdigest() != NODE_S390X_SHA256:
        raise RuntimeError(
            "ATEABLE_JS_RUNTIME: official runtime checksum did not match"
        )

    # Only these worker-owned directories are changed. Never follow an existing
    # runtime-directory symlink or extract archive paths to the filesystem.
    for directory in (binary_dir.parent, binary_dir):
        if directory.is_symlink():
            raise RuntimeError("ATEABLE_JS_RUNTIME: runtime directory is a symlink")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    staged = []
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as package:
            root = f"node-v{NODE_VERSION}-linux-s390x"
            for name, directory, limit, label in (
                (f"{root}/bin/node", binary_dir, MAX_RUNTIME_SIZE, "binary"),
                (
                    f"{root}/LICENSE",
                    binary_dir.parent,
                    MAX_RUNTIME_LICENSE_SIZE,
                    "license",
                ),
            ):
                entries = [item for item in package.getmembers() if item.name == name]
                if len(entries) != 1:
                    raise RuntimeError(
                        f"ATEABLE_JS_RUNTIME: runtime {label} is missing or ambiguous"
                    )
                entry = entries[0]
                if not entry.isfile():
                    raise RuntimeError(
                        f"ATEABLE_JS_RUNTIME: runtime {label} is not a regular file"
                    )
                if not 0 < entry.size <= limit:
                    raise RuntimeError(
                        f"ATEABLE_JS_RUNTIME: extracted runtime {label} exceeds the size limit"
                    )
                source = package.extractfile(entry)
                if source is None:
                    raise RuntimeError(
                        f"ATEABLE_JS_RUNTIME: runtime {label} is missing"
                    )
                with (
                    source,
                    tempfile.NamedTemporaryFile(
                        prefix=".node-", dir=directory, delete=False
                    ) as output,
                ):
                    temporary = Path(output.name)
                    staged.append(temporary)
                    copied = 0
                    while chunk := source.read(65536):
                        copied += len(chunk)
                        if copied > limit:
                            raise RuntimeError(
                                f"ATEABLE_JS_RUNTIME: extracted runtime {label} exceeds the size limit"
                            )
                        output.write(chunk)
                if copied != entry.size:
                    raise RuntimeError(
                        f"ATEABLE_JS_RUNTIME: runtime {label} is truncated"
                    )
        temporary, license_temporary = staged
        temporary.chmod(0o700)
        license_temporary.chmod(0o600)
        result = subprocess.run(
            [str(temporary), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        if result.stdout.strip() != f"v{NODE_VERSION}":
            raise RuntimeError(
                "ATEABLE_JS_RUNTIME: installed runtime version is unexpected"
            )
        binary = binary_dir / "node"
        # Publish the complete notices before the executable. Each replacement
        # is atomic and replaces a destination symlink rather than following it.
        license_temporary.replace(binary_dir.parent / "LICENSE.node")
        temporary.replace(binary)
        return binary
    except (tarfile.TarError, KeyError) as exc:
        raise RuntimeError(
            "ATEABLE_JS_RUNTIME: official archive has no valid runtime binary or license"
        ) from exc
    finally:
        for temporary in staged:
            temporary.unlink(missing_ok=True)


def _fetch(url: str, maximum: int) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "Ateable-worker", "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(maximum + 1)
    if len(data) > maximum:
        raise RuntimeError("ATEABLE_JS_RUNTIME: dependency exceeds the download limit")
    return data


def ensure_worker_runtime(base: Path) -> Path | None:
    """Use an existing runtime or a verified official Deno/Node release.

    This function runs only in the SSH deployment process on the Linux VM.
    No binary is fetched by the controller.
    """
    binary_dir = base / "runtime" / "bin"
    os.environ["PATH"] = str(binary_dir) + os.pathsep + os.environ.get("PATH", "")
    if detect_js_runtimes():
        return None
    if platform.system() == "Linux" and platform.machine().lower() == "s390x":
        return _install_s390x_node(binary_dir)
    architecture = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
    }.get(platform.machine().lower())
    if platform.system() != "Linux" or architecture is None:
        raise RuntimeError(
            "ATEABLE_JS_RUNTIME: install Deno 2.3+ or Node.js 22+ on this VM architecture"
        )
    name = f"deno-{architecture}-unknown-linux-gnu.zip"
    release = json.loads(_fetch(RELEASE_API, 2 * 1024 * 1024))
    asset = next(
        (item for item in release.get("assets", []) if item.get("name") == name), None
    )
    if asset is None or not str(asset.get("digest", "")).startswith("sha256:"):
        raise RuntimeError(
            "ATEABLE_JS_RUNTIME: official runtime checksum is unavailable"
        )
    url = f"https://github.com/denoland/deno/releases/download/v{DENO_VERSION}/{name}"
    archive = _fetch(url, 100 * 1024 * 1024)
    if "sha256:" + hashlib.sha256(archive).hexdigest() != asset["digest"]:
        raise RuntimeError(
            "ATEABLE_JS_RUNTIME: official runtime checksum did not match"
        )
    binary_dir.mkdir(parents=True, exist_ok=True)
    temporary = binary_dir / "deno.tmp"
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as package:
            entry = package.getinfo("deno")
            if entry.file_size > 250 * 1024 * 1024:
                raise RuntimeError(
                    "ATEABLE_JS_RUNTIME: extracted runtime exceeds the size limit"
                )
            with package.open(entry) as source, temporary.open("wb") as output:
                while chunk := source.read(65536):
                    output.write(chunk)
        temporary.chmod(0o700)
        result = subprocess.run(
            [str(temporary), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        if not result.stdout.startswith(f"deno {DENO_VERSION}"):
            raise RuntimeError(
                "ATEABLE_JS_RUNTIME: installed runtime version is unexpected"
            )
        binary = binary_dir / "deno"
        temporary.replace(binary)
        return binary
    finally:
        temporary.unlink(missing_ok=True)
