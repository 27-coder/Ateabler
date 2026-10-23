# One analysis and one shared library directory for four entry points.
# PyInstaller executes this file with Analysis/PYZ/EXE/COLLECT/BUNDLE injected.
import json
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

inputs = json.loads(Path(os.environ["ATEABLE_NATIVE_INPUTS"]).read_text("utf-8"))
source = Path(inputs["source"])
native = source / "packaging" / "native"
names = ("admin_gui", "admin_cli", "node_cli", "node_gui")
entry_paths = {str((native / (name + ".py")).resolve()) for name in names}
datas = [(str(Path(item[0])), item[1]) for item in inputs["datas"]]
for distribution in ("ateable-downloader", "fastapi", "uvicorn", "pydantic"):
    datas += copy_metadata(distribution)

analysis = Analysis(
    [str(native / (name + ".py")) for name in names],
    pathex=[str(source / "backend"), str(native)],
    binaries=[],
    datas=datas,
    hiddenimports=collect_submodules("uvicorn"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "boto3", "botocore", "s3transfer", "jmespath", "yt_dlp", "yt_dlp_ejs",
        "imageio_ffmpeg", "pytest", "ruff", "tkinter", "PyQt5", "PyQt6",
        "PySide2",
    ],
    noarchive=False,
    optimize=0,
)
# Editable-install provenance can expose the builder's private local path.
# Runtime metadata and licence files remain; installer-only URLs are unnecessary.
analysis.datas = [
    item for item in analysis.datas
    if Path(item[0]).name != "direct_url.json"
]
pyz = PYZ(analysis.pure)
common_scripts = [
    item for item in analysis.scripts if str(Path(item[1]).resolve()) not in entry_paths
]


def executable(entry, name, console):
    selected = [
        item for item in analysis.scripts
        if str(Path(item[1]).resolve()) == str((native / (entry + ".py")).resolve())
    ]
    if len(selected) != 1:
        raise RuntimeError("Missing native entry point: " + entry)
    return EXE(
        pyz,
        common_scripts + selected,
        [],
        exclude_binaries=True,
        name=name,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=console,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=inputs["architecture"] if sys.platform == "darwin" else None,
        codesign_identity=None,
        entitlements_file=None,
        contents_directory="_internal",
    )


admin_gui = executable("admin_gui", "Ateable", False)
admin_cli = executable("admin_cli", "ateable-cli", True)
node_cli = executable("node_cli", "ateable-node", True)
node_gui = executable("node_gui", "ateable-node-ui", False)
collection = COLLECT(
    admin_gui, admin_cli, node_cli, node_gui, analysis.binaries, analysis.datas,
    strip=False, upx=False, name="Ateable",
)
if sys.platform == "darwin":
    bundle = BUNDLE(
        collection,
        name="Ateable.app",
        bundle_identifier="org.ateable.admin",
        version=inputs["version"],
        info_plist={
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
            "CFBundleName": "Ateable",
            "CFBundleDisplayName": "Ateable",
        },
    )
