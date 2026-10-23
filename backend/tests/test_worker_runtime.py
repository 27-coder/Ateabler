import hashlib
import io
import json
import os
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from ateable import worker_runtime


def test_runtime_uses_existing_dependency_without_fetching(monkeypatch, tmp_path):
    monkeypatch.setattr(
        worker_runtime,
        "detect_js_runtimes",
        lambda: {"node": {"path": "/usr/bin/node"}},
    )
    monkeypatch.setattr(
        worker_runtime,
        "_fetch",
        lambda *_: pytest.fail("Existing runtime must not trigger a download"),
    )
    monkeypatch.setenv("PATH", "/usr/bin")
    assert worker_runtime.ensure_worker_runtime(tmp_path) is None


@pytest.mark.parametrize("valid_digest", [True, False])
def test_runtime_is_downloaded_and_verified_only_in_worker_directory(
    monkeypatch, tmp_path, valid_digest
):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(worker_runtime, "detect_js_runtimes", dict)
    monkeypatch.setattr(worker_runtime.platform, "system", lambda: "Linux")
    monkeypatch.setattr(worker_runtime.platform, "machine", lambda: "x86_64")
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("deno", b"fixture binary")
    payload = data.getvalue()
    digest = hashlib.sha256(payload).hexdigest() if valid_digest else "wrong"
    release = {
        "assets": [
            {"name": "deno-x86_64-unknown-linux-gnu.zip", "digest": "sha256:" + digest}
        ]
    }
    calls = []

    def fetch(url, maximum):
        calls.append(url)
        return (
            json.dumps(release).encode()
            if url == worker_runtime.RELEASE_API
            else payload
        )

    monkeypatch.setattr(worker_runtime, "_fetch", fetch)
    monkeypatch.setattr(
        worker_runtime.subprocess,
        "run",
        lambda args, **_: subprocess.CompletedProcess(args, 0, "deno 2.8.1\n", ""),
    )
    if valid_digest:
        target = worker_runtime.ensure_worker_runtime(tmp_path)
        assert target == tmp_path / "runtime" / "bin" / "deno"
        assert target.read_bytes() == b"fixture binary"
    else:
        with pytest.raises(RuntimeError, match="checksum"):
            worker_runtime.ensure_worker_runtime(tmp_path)
        assert not (tmp_path / "runtime" / "bin" / "deno").exists()
    assert len(calls) == 2 and all(url.startswith("https://") for url in calls)


def node_archive(
    name=None,
    *,
    content=b"fixture node",
    kind=tarfile.REGTYPE,
    license_name=None,
    license_content=b"Node.js and bundled component license notices\n",
    license_kind=tarfile.REGTYPE,
    include_license=True,
    duplicate_license=False,
):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        entry = tarfile.TarInfo(
            name or f"node-v{worker_runtime.NODE_VERSION}-linux-s390x/bin/node"
        )
        entry.type = kind
        if kind == tarfile.REGTYPE:
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
        else:
            entry.linkname = "../../outside"
            archive.addfile(entry)
        if include_license:
            license_entry = tarfile.TarInfo(
                license_name
                or f"node-v{worker_runtime.NODE_VERSION}-linux-s390x/LICENSE"
            )
            license_entry.type = license_kind
            if license_kind == tarfile.REGTYPE:
                license_entry.size = len(license_content)
                archive.addfile(license_entry, io.BytesIO(license_content))
                if duplicate_license:
                    archive.addfile(license_entry, io.BytesIO(license_content))
            else:
                license_entry.linkname = "../../outside"
                archive.addfile(license_entry)
        unrelated = tarfile.TarInfo("../../outside")
        unrelated.size = 3
        archive.addfile(unrelated, io.BytesIO(b"bad"))
    return data.getvalue()


def configure_node(monkeypatch, payload, *, checksums=None):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(worker_runtime, "detect_js_runtimes", dict)
    monkeypatch.setattr(worker_runtime.platform, "system", lambda: "Linux")
    monkeypatch.setattr(worker_runtime.platform, "machine", lambda: "s390x")
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(worker_runtime, "NODE_S390X_SHA256", digest)
    name = f"node-v{worker_runtime.NODE_VERSION}-linux-s390x.tar.gz"
    if checksums is None:
        checksums = f"{digest}  {name}\n".encode()
    calls = []

    def fetch(url, maximum):
        calls.append((url, maximum))
        return checksums if url.endswith("/SHASUMS256.txt") else payload

    monkeypatch.setattr(worker_runtime, "_fetch", fetch)
    monkeypatch.setattr(
        worker_runtime.subprocess,
        "run",
        lambda args, **_: subprocess.CompletedProcess(
            args, 0, f"v{worker_runtime.NODE_VERSION}\n", ""
        ),
    )
    return calls


def test_s390x_node_is_private_verified_and_extracts_only_exact_binary_and_license(
    monkeypatch, tmp_path
):
    payload = node_archive()
    calls = configure_node(monkeypatch, payload)
    target = worker_runtime.ensure_worker_runtime(tmp_path)
    assert target == tmp_path / "runtime" / "bin" / "node"
    assert target.read_bytes() == b"fixture node"
    assert target.stat().st_mode & 0o777 == 0o700
    assert target.parent.stat().st_mode & 0o777 == 0o700
    assert target.parent.parent.stat().st_mode & 0o777 == 0o700
    assert list(target.parent.iterdir()) == [target]
    license_path = target.parent.parent / "LICENSE.node"
    assert (
        license_path.read_bytes() == b"Node.js and bundled component license notices\n"
    )
    assert license_path.stat().st_mode & 0o777 == 0o600
    assert set(target.parent.parent.iterdir()) == {target.parent, license_path}
    assert not (tmp_path.parent / "outside").exists()
    assert os.environ["PATH"].split(os.pathsep)[0] == str(target.parent)
    assert calls == [
        (f"{worker_runtime.NODE_RELEASE_URL}/SHASUMS256.txt", 128 * 1024),
        (
            (
                f"{worker_runtime.NODE_RELEASE_URL}/node-v{worker_runtime.NODE_VERSION}"
                "-linux-s390x.tar.gz"
            ),
            100 * 1024 * 1024,
        ),
    ]


def test_s390x_node_install_is_idempotent(monkeypatch, tmp_path):
    calls = configure_node(monkeypatch, node_archive())
    target = worker_runtime.ensure_worker_runtime(tmp_path)
    monkeypatch.setattr(
        worker_runtime,
        "detect_js_runtimes",
        lambda: {"node": {"path": str(target)}} if target.exists() else {},
    )
    assert worker_runtime.ensure_worker_runtime(tmp_path) is None
    assert len(calls) == 2


@pytest.mark.parametrize(
    "checksums",
    [
        b"",
        b"invalid manifest",
        b"\xff",
        b"0" * 64 + b"  different-file.tar.gz\n",
        b"0" * 64 + b"  node-v22.23.3-linux-s390x.tar.gz\n",
    ],
)
def test_s390x_node_rejects_missing_or_changed_official_checksum(
    monkeypatch, tmp_path, checksums
):
    calls = configure_node(monkeypatch, node_archive(), checksums=checksums)
    with pytest.raises(RuntimeError, match="checksum"):
        worker_runtime.ensure_worker_runtime(tmp_path)
    assert len(calls) == 1
    assert not (tmp_path / "runtime").exists()


def test_s390x_node_rejects_ambiguous_checksum(monkeypatch, tmp_path):
    payload = node_archive()
    digest = hashlib.sha256(payload).hexdigest()
    line = (
        f"{digest}  node-v{worker_runtime.NODE_VERSION}-linux-s390x.tar.gz\n"
    ).encode()
    calls = configure_node(monkeypatch, payload, checksums=line * 2)
    with pytest.raises(RuntimeError, match="checksum"):
        worker_runtime.ensure_worker_runtime(tmp_path)
    assert len(calls) == 1


def test_s390x_node_rejects_tampered_archive(monkeypatch, tmp_path):
    payload = node_archive()
    configure_node(monkeypatch, payload)
    monkeypatch.setattr(worker_runtime, "NODE_S390X_SHA256", "0" * 64)
    checksums = (
        "0" * 64 + f"  node-v{worker_runtime.NODE_VERSION}-linux-s390x.tar.gz\n"
    ).encode()
    monkeypatch.setattr(
        worker_runtime,
        "_fetch",
        lambda url, _: checksums if url.endswith("SHASUMS256.txt") else payload,
    )
    with pytest.raises(RuntimeError, match="did not match"):
        worker_runtime.ensure_worker_runtime(tmp_path)
    assert not (tmp_path / "runtime").exists()


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../node", tarfile.REGTYPE),
        ("node-v22.23.3-linux-s390x/bin/../bin/node", tarfile.REGTYPE),
        ("node-v22.23.3-linux-x64/bin/node", tarfile.REGTYPE),
        (None, tarfile.SYMTYPE),
        (None, tarfile.LNKTYPE),
    ],
)
def test_s390x_node_rejects_traversal_wrong_architecture_and_links(
    monkeypatch, tmp_path, name, kind
):
    configure_node(monkeypatch, node_archive(name, kind=kind))
    with pytest.raises(RuntimeError, match="missing or ambiguous|regular file"):
        worker_runtime.ensure_worker_runtime(tmp_path)
    assert not (tmp_path / "runtime" / "bin" / "node").exists()
    assert not (tmp_path.parent / "outside").exists()


@pytest.mark.parametrize("content", [b"", b"larger than fixture size bound"])
def test_s390x_node_enforces_extracted_binary_size(monkeypatch, tmp_path, content):
    configure_node(monkeypatch, node_archive(content=content))
    monkeypatch.setattr(worker_runtime, "MAX_RUNTIME_SIZE", 4)
    with pytest.raises(RuntimeError, match="size limit"):
        worker_runtime.ensure_worker_runtime(tmp_path)
    assert list((tmp_path / "runtime" / "bin").iterdir()) == []


@pytest.mark.parametrize("version", ["v20.0.0\n", "v22.23.30\n", "v22.23.3 extra\n"])
def test_s390x_node_checks_exact_version_before_replacing_existing_file(
    monkeypatch, tmp_path, version
):
    configure_node(monkeypatch, node_archive())
    target = tmp_path / "runtime" / "bin" / "node"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"previous binary")
    license_path = target.parent.parent / "LICENSE.node"
    license_path.write_bytes(b"previous license")
    monkeypatch.setattr(
        worker_runtime.subprocess,
        "run",
        lambda args, **_: subprocess.CompletedProcess(args, 0, version, ""),
    )
    with pytest.raises(RuntimeError, match="version is unexpected"):
        worker_runtime.ensure_worker_runtime(tmp_path)
    assert target.read_bytes() == b"previous binary"
    assert license_path.read_bytes() == b"previous license"
    assert list(target.parent.iterdir()) == [target]
    assert not list((tmp_path / "runtime").rglob(".node-*"))


@pytest.mark.parametrize(
    "error", [subprocess.TimeoutExpired("node", 10), OSError("bad binary")]
)
def test_s390x_node_cleans_temporary_binary_if_execution_fails(
    monkeypatch, tmp_path, error
):
    configure_node(monkeypatch, node_archive())

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(worker_runtime.subprocess, "run", fail)
    with pytest.raises(type(error)):
        worker_runtime.ensure_worker_runtime(tmp_path)
    assert list((tmp_path / "runtime" / "bin").iterdir()) == []
    assert not (tmp_path / "runtime" / "LICENSE.node").exists()
    assert not list((tmp_path / "runtime").rglob(".node-*"))


@pytest.mark.parametrize(
    "options",
    [
        {"include_license": False},
        {"license_name": "../LICENSE"},
        {"license_name": "node-v22.23.3-linux-s390x/bin/../LICENSE"},
        {"license_name": "node-v22.23.3-linux-x64/LICENSE"},
        {"license_kind": tarfile.SYMTYPE},
        {"license_kind": tarfile.LNKTYPE},
        {"license_kind": tarfile.DIRTYPE},
        {"duplicate_license": True},
    ],
)
def test_s390x_node_requires_one_exact_regular_license(monkeypatch, tmp_path, options):
    configure_node(monkeypatch, node_archive(**options))
    monkeypatch.setattr(
        worker_runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("Invalid license must stop installation"),
    )
    with pytest.raises(RuntimeError, match="license"):
        worker_runtime.ensure_worker_runtime(tmp_path)
    assert not (tmp_path / "runtime" / "bin" / "node").exists()
    assert not (tmp_path / "runtime" / "LICENSE.node").exists()
    assert not list((tmp_path / "runtime").rglob(".node-*"))
    assert not (tmp_path.parent / "outside").exists()


@pytest.mark.parametrize("content", [b"", b"larger than license size bound"])
def test_s390x_node_bounds_license_size_and_cleans_staged_binary(
    monkeypatch, tmp_path, content
):
    configure_node(monkeypatch, node_archive(license_content=content))
    monkeypatch.setattr(worker_runtime, "MAX_RUNTIME_LICENSE_SIZE", 4)
    with pytest.raises(RuntimeError, match="license.*size limit"):
        worker_runtime.ensure_worker_runtime(tmp_path)
    assert not (tmp_path / "runtime" / "bin" / "node").exists()
    assert not (tmp_path / "runtime" / "LICENSE.node").exists()
    assert not list((tmp_path / "runtime").rglob(".node-*"))


def test_s390x_node_replaces_license_symlink_without_following_it(
    monkeypatch, tmp_path
):
    configure_node(monkeypatch, node_archive())
    outside = tmp_path / "outside-license"
    outside.write_bytes(b"unrelated license")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    license_path = runtime / "LICENSE.node"
    license_path.symlink_to(outside)
    worker_runtime.ensure_worker_runtime(tmp_path)
    assert outside.read_bytes() == b"unrelated license"
    assert not license_path.is_symlink()
    assert (
        license_path.read_bytes() == b"Node.js and bundled component license notices\n"
    )


@pytest.mark.parametrize("destination", ["LICENSE.node", "node"])
def test_s390x_node_publication_failure_preserves_binary_and_cleans_temps(
    monkeypatch, tmp_path, destination
):
    configure_node(monkeypatch, node_archive())
    target = tmp_path / "runtime" / "bin" / "node"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"previous binary")
    license_path = target.parent.parent / "LICENSE.node"
    license_path.write_bytes(b"previous license")
    original_replace = Path.replace

    def replace(source, target_path):
        if Path(target_path).name == destination:
            raise OSError("fixture publication failure")
        return original_replace(source, target_path)

    monkeypatch.setattr(Path, "replace", replace)
    with pytest.raises(OSError, match="publication failure"):
        worker_runtime.ensure_worker_runtime(tmp_path)
    assert target.read_bytes() == b"previous binary"
    expected_license = (
        b"previous license"
        if destination == "LICENSE.node"
        else b"Node.js and bundled component license notices\n"
    )
    assert license_path.read_bytes() == expected_license
    assert not list((tmp_path / "runtime").rglob(".node-*"))


@pytest.mark.parametrize("component", ["runtime", "bin"])
def test_s390x_node_refuses_symlink_runtime_directories(
    monkeypatch, tmp_path, component
):
    configure_node(monkeypatch, node_archive())
    outside = tmp_path / "outside"
    outside.mkdir()
    if component == "runtime":
        (tmp_path / "runtime").symlink_to(outside, target_is_directory=True)
    else:
        (tmp_path / "runtime").mkdir()
        (tmp_path / "runtime" / "bin").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symlink"):
        worker_runtime.ensure_worker_runtime(tmp_path)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    "system,architecture", [("Darwin", "s390x"), ("Linux", "riscv64")]
)
def test_runtime_rejects_unsupported_platform_without_fetching(
    monkeypatch, tmp_path, system, architecture
):
    monkeypatch.setattr(worker_runtime, "detect_js_runtimes", dict)
    monkeypatch.setattr(worker_runtime.platform, "system", lambda: system)
    monkeypatch.setattr(worker_runtime.platform, "machine", lambda: architecture)
    monkeypatch.setattr(worker_runtime, "_fetch", lambda *_: pytest.fail("unsupported"))
    with pytest.raises(RuntimeError, match="VM architecture"):
        worker_runtime.ensure_worker_runtime(tmp_path)


@pytest.mark.parametrize("length", [3, 4, 5])
def test_dependency_fetch_is_bounded(monkeypatch, length):
    def open_url(request, timeout):
        assert request.full_url == worker_runtime.NODE_RELEASE_URL + "/SHASUMS256.txt"
        assert timeout == 60
        return io.BytesIO(b"a" * length)

    monkeypatch.setattr(worker_runtime.urllib.request, "urlopen", open_url)
    if length > 4:
        with pytest.raises(RuntimeError, match="download limit"):
            worker_runtime._fetch(
                worker_runtime.NODE_RELEASE_URL + "/SHASUMS256.txt", 4
            )
    else:
        assert (
            worker_runtime._fetch(
                worker_runtime.NODE_RELEASE_URL + "/SHASUMS256.txt", 4
            )
            == b"a" * length
        )
