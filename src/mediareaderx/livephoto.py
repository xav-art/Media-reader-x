"""Find the motion clip that belongs to an iPhone Live Photo.

iOS exports a Live Photo as IMG_1234.HEIC (or .JPG) plus IMG_1234.MOV in the
same folder. We pair them by stem, case-insensitively.
"""

from __future__ import annotations

from pathlib import Path

from mediareaderx.formats import MediaKind, kind_of

_MOTION_EXTS = (".mov", ".mp4")


def find_motion_companion(image_path: str | Path) -> Path | None:
    path = Path(image_path)
    if kind_of(path) is not MediaKind.IMAGE:
        return None
    stem = path.stem.lower()
    try:
        for candidate in path.parent.iterdir():
            if (candidate.stem.lower() == stem and candidate.suffix.lower() in _MOTION_EXTS
                    and candidate.is_file()):
                return candidate
    except OSError:
        return None
    return None
