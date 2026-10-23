"""Source pack determinism, curation and target-install safety."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import types
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


builder = module(ROOT / "scripts/build-install-packs.py", "install_pack_builder")
installer = module(ROOT / "packaging/install/install.py", "install_pack_installer")


@pytest.fixture(scope="module")
def packs(tmp_path_factory):
    return builder.build(ROOT, tmp_path_factory.mktemp("install-packs").resolve())


def contents(path: Path) -> dict[str, bytes]:
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            assert archive.testzip() is None
            return {
                name.partition("/")[2]: archive.read(name)
                for name in archive.namelist()
            }
    with tarfile.open(path) as archive:
        assert all(item.isfile() and not item.issym() for item in archive)
        return {
            item.name.partition("/")[2]: archive.extractfile(item).read()
            for item in archive
        }


def unpack(files: dict[str, bytes], destination: Path):
    for name, data in files.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return destination


def options(prefix: Path, **values):
    return argparse.Namespace(
        prefix=prefix,
        role="node",
        gui=False,
        offline=True,
        wheelhouse=None,
        dry_run=True,
        **values,
    )


def host_pack(packs):
    return next(
        path for path in packs if f"-{installer.SYSTEMS[sys.platform]}." in path.name
    )


def test_packs_are_deterministic_and_have_curated_payloads(packs, tmp_path):
    second = builder.build(ROOT, tmp_path.resolve() / "second")
    for first, rebuilt in zip(packs, second, strict=True):
        assert first.read_bytes() == rebuilt.read_bytes()
        files = contents(first)
        assert set(files).issuperset(
            {"install.py", "launch.py", "pack.json", "SHA256SUMS", "docs/WINDOWS-SSH.md"}
        )
        metadata = json.loads(files["pack.json"])
        assert all(
            hashlib.sha256(files[name]).hexdigest() == hashed
            for name, hashed in metadata["files"].items()
        )
        assert not any(
            name.endswith((".env", ".db", ".pyc"))
            or "/tests/" in name
            or "credentials" in name
            for name in files
        )
        assert metadata["platform"] in builder.PLATFORMS
        with zipfile.ZipFile(io.BytesIO(files[metadata["wheel"]])) as wheel:
            names = wheel.namelist()
            assert "ateable/ssh_devices.py" in names
            assert "pyproject.toml" in names
            assert all(not name.endswith((".env", ".db", ".pyc")) for name in names)
            wheel_meta = wheel.read(
                next(name for name in names if name.endswith("/METADATA"))
            ).decode()
            assert "Provides-Extra: worker" in wheel_meta
            assert "Provides-Extra: test" not in wheel_meta


def test_node_wheel_is_narrow_and_has_no_base_dependencies(packs):
    files = contents(packs[0])
    metadata = json.loads(files["pack.json"])
    with zipfile.ZipFile(io.BytesIO(files[metadata["node_wheel"]])) as wheel:
        package = {name for name in wheel.namelist() if name.startswith("ateable/")}
        assert package == {
            "ateable/__init__.py",
            "ateable/participant.py",
            "ateable/participant_ui.py",
            "ateable/branding.py",
            "ateable/assets/logo-white-on-black.png",
        }
        meta = wheel.read(
            next(name for name in wheel.namelist() if name.endswith("/METADATA"))
        ).decode()
        assert all(
            'extra == "desktop"' in line
            for line in meta.splitlines()
            if line.startswith("Requires-Dist:")
        )
        entries = wheel.read(
            next(
                name for name in wheel.namelist() if name.endswith("/entry_points.txt")
            )
        ).decode()
        assert "ateable-node = ateable.participant:main" in entries
        assert "ateable-api" not in entries
        assert "ateable.cli" not in entries


def test_dry_run_creates_no_prefix_and_checks_host(packs, tmp_path):
    pack = unpack(contents(host_pack(packs)), tmp_path.resolve() / "pack")
    prefix = tmp_path.resolve() / "New Private Install"
    assert installer.install(options(prefix), pack=pack) == []
    assert not prefix.exists()
    other = next(path for path in packs if path != host_pack(packs))
    wrong = unpack(contents(other), tmp_path.resolve() / "wrong")
    with pytest.raises(ValueError, match="cannot install"):
        installer.install(options(prefix), pack=wrong)


def test_checksum_failure_is_before_any_install(packs, tmp_path):
    pack = unpack(contents(host_pack(packs)), tmp_path.resolve() / "pack")
    (pack / "launch.py").write_text("tampered")
    prefix = tmp_path.resolve() / "untouched"
    with pytest.raises(ValueError, match="checksum"):
        installer.install(options(prefix), pack=pack)
    assert not prefix.exists()


def test_existing_directory_and_symlink_are_refused(packs, tmp_path):
    pack = unpack(contents(host_pack(packs)), tmp_path.resolve() / "pack")
    prefix = tmp_path.resolve() / "unrelated"
    prefix.mkdir()
    kept = prefix / "myfile"
    kept.write_text("keep")
    with pytest.raises(ValueError, match="already exists"):
        installer.install(options(prefix), pack=pack)
    assert kept.read_text() == "keep"
    symlink = tmp_path.resolve() / "link"
    try:
        symlink.symlink_to(prefix, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink privilege unavailable")
    with pytest.raises(ValueError, match="Symbolic link"):
        installer.install(options(symlink / "new"), pack=pack)
    assert not (prefix / "new").exists()


def test_offline_dependency_roles_require_wheelhouse(packs, tmp_path):
    pack = unpack(contents(host_pack(packs)), tmp_path.resolve() / "pack")
    args = options(tmp_path.resolve() / "new")
    args.role = "admin"
    with pytest.raises(ValueError, match="require --wheelhouse"):
        installer.install(args, pack=pack)
    args.role, args.gui = "node", True
    with pytest.raises(ValueError, match="require --wheelhouse"):
        installer.install(args, pack=pack)


def test_real_offline_node_install_handles_spaces_and_never_runs_app(packs, tmp_path):
    pack = unpack(contents(host_pack(packs)), tmp_path.resolve() / "pack")
    prefix = tmp_path.resolve() / "Node's private space"
    args = options(prefix)
    args.dry_run = False
    launchers = installer.install(args, pack=pack)
    assert len(launchers) == 1
    assert not (prefix / "state").exists()
    assert not (prefix / "resources").exists()
    assert (prefix / "docs/WINDOWS-SSH.md").read_bytes() == (
        pack / "docs/WINDOWS-SSH.md"
    ).read_bytes()
    python = prefix / (
        "venv/Scripts/python.exe" if sys.platform == "win32" else "venv/bin/python"
    )
    result = subprocess.run(
        [str(python), str(prefix / "launch.py"), "node", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "workspaces" in result.stdout
    denied = subprocess.run(
        [str(python), str(prefix / "launch.py"), "admin", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert denied.returncode != 0 and "unavailable" in denied.stderr
    check = subprocess.run(
        [str(python), "-m", "pip", "check"], check=False, capture_output=True, text=True
    )
    assert check.returncode == 0, check.stdout
    if sys.platform != "win32":
        help_result = subprocess.run(
            [str(launchers[0]), "--help"], check=False, capture_output=True, text=True
        )
        assert help_result.returncode == 0, help_result.stderr
        assert prefix.stat().st_mode & 0o077 == 0
    assert not (prefix / "state").exists()


def test_offline_node_install_uses_existing_pip_when_ensurepip_is_missing(
    packs, tmp_path, monkeypatch
):
    pack = unpack(contents(host_pack(packs)), tmp_path.resolve() / "pack")
    prefix = tmp_path.resolve() / "Node without bundled pip"
    args = options(prefix)
    args.dry_run = False
    actual_run = subprocess.run
    calls = []

    def without_ensurepip(command, **kwargs):
        calls.append(command)
        if command[1:3] == ["-m", "ensurepip"]:
            return subprocess.CompletedProcess(
                command, 1, "", "No module named ensurepip"
            )
        return actual_run(command, **kwargs)

    monkeypatch.setattr(installer.subprocess, "run", without_ensurepip)
    installer.install(args, pack=pack)
    python = prefix / (
        "venv/Scripts/python.exe" if sys.platform == "win32" else "venv/bin/python"
    )
    installs = [command for command in calls if "install" in command]
    assert len(installs) == 1
    assert installs[0][:7] == [
        sys.executable,
        "-m",
        "pip",
        "--isolated",
        "--python",
        str(python),
        "install",
    ]
    assert "--no-index" in installs[0]
    assert (
        actual_run(
            [str(python), str(prefix / "launch.py"), "node", "--help"],
            capture_output=True,
        ).returncode
        == 0
    )
    check = actual_run(
        [sys.executable, "-m", "pip", "--python", str(python), "check"],
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stdout + check.stderr
    assert not (prefix / "state").exists()


def test_unavailable_local_pip_fails_and_removes_only_its_new_prefix(
    packs, tmp_path, monkeypatch
):
    pack = unpack(contents(host_pack(packs)), tmp_path.resolve() / "pack")
    prefix = tmp_path.resolve() / "failed new installation"
    unrelated = tmp_path / "keep.txt"
    unrelated.write_text("keep")
    args = options(prefix)
    args.dry_run = False
    calls = []

    def unavailable(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", "unavailable")

    monkeypatch.setattr(installer.subprocess, "run", unavailable)
    with pytest.raises(ValueError, match="pip 22.3"):
        installer.install(args, pack=pack)
    assert all("install" not in command for command in calls)
    assert not prefix.exists()
    assert unrelated.read_text() == "keep"


@pytest.mark.parametrize("role", ["node", "worker"])
def test_node_gui_launcher_calls_credential_picker_entry_point(
    tmp_path, monkeypatch, role
):
    launcher = module(ROOT / "packaging/install/launch.py", f"install_launcher_{role}")
    prefix = tmp_path.resolve() / role
    prefix.mkdir()
    (prefix / "install.json").write_text(json.dumps({"role": role, "gui": True}))
    monkeypatch.setattr(launcher, "__file__", str(prefix / "launch.py"))
    monkeypatch.setattr(launcher, "os", types.SimpleNamespace(environ={}))
    monkeypatch.setattr(
        launcher,
        "sys",
        types.SimpleNamespace(
            argv=["launch.py", "node-ui", "--session-file", "space path.json"]
        ),
    )
    fake_ui = types.ModuleType("ateable.participant_ui")
    received = []
    fake_ui.main = lambda args: received.append(args) or 17
    monkeypatch.setitem(sys.modules, "ateable.participant_ui", fake_ui)
    assert launcher.main() == 17
    assert received == [["--session-file", "space path.json"]]
    assert launcher.os.environ["ATEABLE_EMBEDDED_WORKER"] == "false"
    (prefix / "install.json").write_text(json.dumps({"role": role, "gui": False}))
    launcher.sys.argv = ["launch.py", "node-ui"]
    with pytest.raises(SystemExit, match="unavailable"):
        launcher.main()
