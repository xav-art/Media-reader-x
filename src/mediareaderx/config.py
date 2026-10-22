"""Per-user paths and safety limits. Nothing here needs admin rights."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from mediareaderx import APP_ID

# Files larger than this are refused before any decoder touches them.
MAX_IMAGE_FILE_BYTES = 1 * 1024**3
# Pillow raises DecompressionBombError above this pixel count (~ 24k x 24k).
MAX_IMAGE_PIXELS = 576_000_000
# Cap decoded animation frames so a hostile GIF/WebP cannot exhaust memory.
# Review measured a 426 KB GIF pinning 772 MB under the old 1.5 GB cap; 384 MB keeps
# an 8 GB work PC comfortable while still playing ordinary animations in full.
MAX_ANIMATION_FRAMES = 300
MAX_ANIMATION_BYTES = 384 * 1024**2


# Data folder used by 1.0, when the app was called "Media Reader".
LEGACY_APP_ID = "MediaReader"


def _local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local"))


def data_dir() -> Path:
    """Return %LOCALAPPDATA%\\MediaReaderX (created on demand)."""
    path = _local_appdata() / APP_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def settings_file() -> Path:
    return data_dir() / "settings.ini"


def migrate_legacy_settings() -> bool:
    """Copy 1.0 settings (window layout, volume, last folder) on first run."""
    new, old = settings_file(), _local_appdata() / LEGACY_APP_ID / "settings.ini"
    if new.exists() or not old.is_file():
        return False
    shutil.copyfile(old, new)
    return True
