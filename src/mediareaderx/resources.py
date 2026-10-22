"""Locate bundled resources both from source and inside a PyInstaller build."""

from __future__ import annotations

import sys
from pathlib import Path


def resource_dir() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / "mediareaderx" / "assets"
    return Path(__file__).resolve().parent / "assets"


def icon_path() -> Path | None:
    path = resource_dir() / "app.ico"
    return path if path.is_file() else None
