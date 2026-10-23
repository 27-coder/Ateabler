"""Ateable's approved mark, shared by the Admin and optional Node viewer."""

from __future__ import annotations

from pathlib import Path


def logo_path() -> Path:
    return Path(__file__).with_name("assets") / "logo-white-on-black.png"


def logo_pixmap(size: int):
    # Keep the headless Node client free of Qt imports.
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPixmap

    return QPixmap(str(logo_path())).scaled(
        size,
        size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def app_icon():
    from PySide6.QtGui import QIcon

    return QIcon(str(logo_path()))
