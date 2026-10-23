"""Packaging boundaries and real archive contents without a compiler."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import runpy
import sys
import tarfile
import tomllib
import types
import zipfile
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "native_builder", SOURCE / "scripts/build-native.py"
)
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


def test_console_scripts_do_not_collide_on_case_insensitive_filesystems():
    project = tomllib.loads((SOURCE / "backend/pyproject.toml").read_text())
    scripts = list(project["project"]["scripts"])
    assert len({name.casefold() for name in scripts}) == len(scripts), (
        "Console scripts must have distinct names on Windows and macOS; "
        "case-only aliases break pip installation/removal"
    )


@pytest.mark.parametrize(
    "system,machine,expected",
    [
        ("Windows", "AMD64", ("windows", "x86_64")),
        ("Darwin", "arm64", ("macos", "arm64")),
        ("Darwin", "x86_64", ("macos", "x86_64")),
        ("Linux", "aarch64", ("linux", "arm64")),
    ],
)
def test_platform_names(system, machine, expected):
    assert native.platform_target(system, machine) == expected


@pytest.mark.parametrize(
    "system,machine", [("Android", "arm64"), ("Linux", "i686"), ("Windows", "ARM64")]
)
def test_unsupported_native_targets_are_explicit(system, machine):
    with pytest.raises(ValueError):
        native.platform_target(system, machine)


def test_worker_and_browser_resource_allowlist(tmp_path):
    native.prepare_resources(SOURCE, tmp_path / "resources")
    files = {
        path.relative_to(tmp_path / "resources").as_posix()
        for path in (tmp_path / "resources").rglob("*")
        if path.is_file()
    }
    assert "worker-source/ateable/remote_worker.py" in files
    assert "worker-source/ateable/assets/logo-white-on-black.png" in files
    assert "ateable/assets/logo-white-on-black.png" in files
    assert "docs/QUICKSTART.md" in files
    for name in ("CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md"):
        assert "docs/" + name in files
    assert "docs/docs/WINDOWS-SSH.md" in files
    assert "docs/WINDOWS-SSH.md" in (
        tmp_path / "resources/docs/QUICKSTART.md"
    ).read_text()
    assert "browser-extension/manifest.firefox.json" in files
    assert "licenses/qt-6.8.3/QtBase-6.8.3-NOTICES.txt" in files
    assert "licenses/qt-6.8.3/QtForPython-6.8.3-NOTICES.txt" in files
    assert "licenses/pyinstaller/PyInstaller-6.22.3-COPYING.txt" in files
    assert "outputs/browser-extensions/ateable-capture-firefox-1.1.0.xpi" in files
    assert all(
        not any(
            part in name
            for part in (".env", "credentials", ".sqlite", "__pycache__", "/tests/")
        )
        for name in files
    )
    with zipfile.ZipFile(
        tmp_path
        / "resources/outputs/browser-extensions/ateable-capture-firefox-1.1.0.zip"
    ) as archive:
        assert "scripts" in json.loads(archive.read("manifest.json"))["background"]


def test_vendored_qt_notices_preserve_every_original_text_and_provenance():
    root = SOURCE / "packaging/native/licenses"
    entries = json.loads((root / "SOURCES.json").read_text())
    assert len(entries) > 80
    for entry in entries:
        data = (root / f"{entry['component']}-6.8.3-NOTICES.txt").read_bytes()
        marker = ("Source: " + entry["source"] + "\n" + "=" * 78 + "\n").encode()
        start = data.index(marker) + len(marker)
        original = data[start : start + entry["bytes"]]
        assert hashlib.sha256(original).hexdigest() == entry["sha256"]
    for component in ("QtBase", "QtForPython"):
        text = (root / f"{component}-6.8.3-NOTICES.txt").read_text()
        assert "GNU LESSER GENERAL PUBLIC LICENSE" in text
        assert "GNU GENERAL PUBLIC LICENSE" in text


def test_vendored_bootloader_license_includes_exception_and_provenance():
    root = SOURCE / "packaging/native/licenses"
    data = (root / "PyInstaller-6.22.3-COPYING.txt").read_bytes()
    entry = json.loads((root / "PyInstaller-6.22.3-SOURCE.json").read_text())
    assert len(data) == entry["bytes"]
    assert hashlib.sha256(data).hexdigest() == entry["sha256"]
    assert b"Bootloader Exception" in data
    assert entry["source"].startswith("https://raw.githubusercontent.com/pyinstaller/")


def test_missing_exact_python_license_stops_build(monkeypatch, tmp_path):
    pyside = types.SimpleNamespace(
        metadata={"Name": "PySide6-Essentials"}, version="6.8.3", files=[]
    )
    monkeypatch.setattr(native.importlib.metadata, "distributions", lambda: [pyside])
    monkeypatch.setattr(native.sys, "base_prefix", str(tmp_path / "missing-python"))
    monkeypatch.setattr(
        native.sysconfig, "get_path", lambda key: str(tmp_path / "missing-stdlib")
    )
    with pytest.raises(ValueError, match="interpreter's LICENSE.txt is missing"):
        native.dependency_metadata(tmp_path)


def test_reject_installed_worker_dependencies(monkeypatch, tmp_path):
    worker = types.SimpleNamespace(metadata={"Name": "yt-dlp"}, version="test")
    monkeypatch.setattr(native.importlib.metadata, "distributions", lambda: [worker])
    with pytest.raises(ValueError, match="Worker-only distribution"):
        native.dependency_metadata(tmp_path)


def test_pyinstaller_argv_uses_own_python_without_shell(tmp_path):
    command = native.pyinstaller_command(
        SOURCE, tmp_path / "work with space", tmp_path / "dist"
    )
    assert command[:3] == [sys.executable, "-m", "PyInstaller"]
    assert command[command.index("--workpath") + 1] == str(
        tmp_path / "work with space/pyinstaller"
    )
    assert command[-1] == str(SOURCE / "packaging/native/ateable.spec")


def test_linux_archive_preserves_executable_and_library_symlinks(tmp_path):
    root = tmp_path / "Ateable-pack"
    root.mkdir()
    executable = root / "ateable-node"
    executable.write_text("test")
    executable.chmod(0o755)
    try:
        (root / "library.so").symlink_to("ateable-node")
    except OSError:
        pytest.skip("Host cannot create symbolic links")
    output = tmp_path / "pack.tar.gz"
    native.make_archive(root, output, "linux")
    with tarfile.open(output) as archive:
        assert archive.getmember("Ateable-pack/ateable-node").mode & 0o111
        assert archive.getmember("Ateable-pack/library.so").issym()
        assert archive.getmember("Ateable-pack/library.so").linkname == "ateable-node"


def test_windows_zip_uses_portable_relative_paths(tmp_path):
    root = tmp_path / "Ateable-pack"
    (root / "Ateable/_internal").mkdir(parents=True)
    (root / "Ateable/ateable-cli.exe").write_bytes(b"binary")
    (root / "Ateable/_internal/shared.dll").write_bytes(b"library")
    output = tmp_path / "pack.zip"
    native.make_archive(root, output, "windows")
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {
            "Ateable-pack/Ateable/ateable-cli.exe",
            "Ateable-pack/Ateable/_internal/shared.dll",
        }
        assert archive.testzip() is None


def test_smoke_commands_keep_gui_console_and_temp_state_separate(tmp_path):
    commands = []

    def runner(command, *, environment):
        commands.append(command)
        assert environment["QT_QPA_PLATFORM"] == "offscreen"
        assert Path(environment["ATEABLE_CONFIG_DIR"]).is_dir()
        assert environment["ATEABLE_DATA_DIR"] == environment["ATEABLE_CONFIG_DIR"]

    native.smoke(tmp_path, "windows", runner)
    assert [(Path(command[0]).name, command[1]) for command in commands] == [
        ("ateable-cli.exe", "--help"),
        ("ateable-node.exe", "--help"),
        ("Ateable.exe", "--native-smoke"),
        ("ateable-node-ui.exe", "--native-smoke"),
    ]


def test_shared_spec_has_single_entry_script_and_preserves_runtime_hooks(
    monkeypatch, tmp_path
):
    inputs = tmp_path / "inputs.json"
    inputs.write_text(
        json.dumps(
            {
                "source": str(SOURCE),
                "datas": [],
                "version": "0.1.0",
                "architecture": "x86_64",
            }
        )
    )
    monkeypatch.setenv("ATEABLE_NATIVE_INPUTS", str(inputs))
    hooks = types.ModuleType("PyInstaller.utils.hooks")
    hooks.collect_submodules = lambda name: [name]
    hooks.copy_metadata = lambda name: []
    for name in ("PyInstaller", "PyInstaller.utils"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "PyInstaller.utils.hooks", hooks)
    built = []
    collected = []

    def analysis(scripts, **kwargs):
        assert "yt_dlp" in kwargs["excludes"] and "boto3" in kwargs["excludes"]
        return types.SimpleNamespace(
            pure=[],
            binaries=[],
            datas=[
                ("ateable_downloader.dist-info/direct_url.json", "private", "DATA"),
                ("ateable_downloader.dist-info/METADATA", "metadata", "DATA"),
            ],
            scripts=[
                ("runtimehook", str(tmp_path / "runtimehook.py"), "PYSOURCE"),
                *((Path(path).stem, path, "PYSOURCE") for path in scripts),
            ],
        )

    def executable(pyz, scripts, unused, **kwargs):
        built.append((scripts, kwargs))
        return kwargs["name"]

    def collect(*args, **kwargs):
        collected.extend(args[-1])
        return "collection"

    runpy.run_path(
        str(SOURCE / "packaging/native/ateable.spec"),
        init_globals={
            "Analysis": analysis,
            "PYZ": lambda pure: "pyz",
            "EXE": executable,
            "COLLECT": collect,
            "BUNDLE": lambda *args, **kwargs: "bundle",
        },
    )
    assert [kwargs["name"] for _, kwargs in built] == list(native.ENTRY_NAMES)
    assert [kwargs["console"] for _, kwargs in built] == [False, True, True, False]
    assert len({kwargs["name"].lower() for _, kwargs in built}) == 4
    assert collected == [
        ("ateable_downloader.dist-info/METADATA", "metadata", "DATA")
    ]
    for scripts, kwargs in built:
        assert len(scripts) == 2 and scripts[0][0] == "runtimehook"
        assert kwargs["exclude_binaries"]


def test_build_archive_checksums_and_manifest_with_fake_compiler(monkeypatch, tmp_path):
    monkeypatch.setattr(native, "platform_target", lambda: ("linux", "x86_64"))

    def dependencies(destination):
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "DEPENDENCIES.json").write_text("[]")
        return [], []

    monkeypatch.setattr(native, "dependency_metadata", dependencies)
    calls = []

    def fake_run(command, *, environment=None):
        calls.append(command)
        distribution = Path(command[command.index("--distpath") + 1]) / "Ateable"
        distribution.mkdir(parents=True)
        for name in native.ENTRY_NAMES:
            (distribution / name).write_bytes(b"fake native binary")
        assert (
            json.loads(Path(environment["ATEABLE_NATIVE_INPUTS"]).read_text())[
                "architecture"
            ]
            == "x86_64"
        )

    monkeypatch.setattr(native, "run", fake_run)
    monkeypatch.setattr(
        native, "smoke", lambda root, target: calls.append(["smoke", str(root), target])
    )
    result = native.build(SOURCE, tmp_path / "output", tmp_path / "work")
    assert result["smoke_tested"]
    artifact = Path(result["artifacts"][0]["path"])
    assert artifact.name == "ateable-native-0.1.0-linux-x86_64.tar.gz"
    assert result["artifacts"][0]["sha256"] == native.digest(artifact)
    checksum = (
        tmp_path / "output/ateable-native-0.1.0-linux-x86_64.sha256"
    ).read_text()
    assert checksum == f"{native.digest(artifact)}  {artifact.name}\n"
    with tarfile.open(artifact) as archive:
        manifest = json.load(
            archive.extractfile("ateable-native-0.1.0-linux-x86_64/build-manifest.json")
        )
        assert not manifest["worker_payload_dependencies_bundled"]
        assert manifest["entrypoints"] == list(native.ENTRY_NAMES)
    with pytest.raises(ValueError, match="already exists"):
        native.build(SOURCE, tmp_path / "output", tmp_path / "work")
