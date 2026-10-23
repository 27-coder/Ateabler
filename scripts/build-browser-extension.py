#!/usr/bin/env python3
"""Build deterministic local-development Chrome/Edge ZIP and Firefox ZIP/XPI.

No browser, signing, installation, dependency download, or publishing is done.
Only listed packaged assets are included; tests and local state are excluded.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import tempfile
import zipfile
from pathlib import Path

ASSETS = (
    "README.md",
    "background.js",
    "core.js",
    "extension-api.js",
    "options.html",
    "options.js",
    "popup.html",
    "popup.js",
    "style.css",
    "ui.js",
)


def build(source: Path, browser: str) -> tuple[bytes, str]:
    manifest_name = "manifest.firefox.json" if browser == "firefox" else "manifest.json"
    manifest = json.loads((source / manifest_name).read_text(encoding="utf-8"))
    if manifest.get("manifest_version") != 3:
        raise ValueError("Expected a Manifest V3 extension")
    if browser == "firefox":
        if "service_worker" in manifest["background"] or not manifest["background"].get(
            "scripts"
        ):
            raise ValueError("Firefox package must use background scripts")
        required = manifest["browser_specific_settings"]["gecko"][
            "data_collection_permissions"
        ]["required"]
        if not {"authenticationInfo", "browsingActivity", "websiteContent"}.issubset(
            required
        ):
            raise ValueError(
                "Firefox package must disclose transmitted capture metadata and token"
            )
    elif "service_worker" not in manifest["background"]:
        raise ValueError("Chrome/Edge package must use a background service worker")
    version = str(manifest["version"])
    if not version or any(character not in "0123456789." for character in version):
        raise ValueError("Extension version must contain digits and periods only")
    entries = {name: (source / name).read_bytes() for name in ASSETS}
    entries["manifest.json"] = (
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    content = io.BytesIO()
    with zipfile.ZipFile(
        content, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for name, data in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data, compresslevel=9)
    return content.getvalue(), version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--browser", choices=("chrome", "firefox", "both"), default="both"
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "browser-extension",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args()
    arguments.output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    targets = (
        ("chrome", "firefox") if arguments.browser == "both" else (arguments.browser,)
    )
    for browser in targets:
        data, version = build(arguments.source_dir, browser)
        suffixes = ("zip", "xpi") if browser == "firefox" else ("zip",)
        for suffix in suffixes:
            destination = (
                arguments.output_dir / f"ateable-capture-{browser}-{version}.{suffix}"
            )
            with tempfile.NamedTemporaryFile(
                dir=arguments.output_dir, prefix=".capture-build-", delete=False
            ) as output:
                temporary = Path(output.name)
                output.write(data)
            try:
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
            results.append(
                {
                    "browser": browser,
                    "path": str(destination.resolve()),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                    "signed": False,
                }
            )
    print(json.dumps({"packages": results}, indent=2))


if __name__ == "__main__":
    main()
