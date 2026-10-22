"""Folder listing and previous/next navigation, sorted like File Explorer."""

from __future__ import annotations

import re
from pathlib import Path

from mediareaderx.formats import ALL_EXTS, IMAGE_EXTS, VIDEO_EXTS

_DIGITS = re.compile(r"(\d+)")
_MOTION_EXTS = (".mov", ".mp4")


def natural_key(path: Path) -> list:
    """IMG_2.heic sorts before IMG_10.heic, case-insensitively (Explorer order)."""
    return [int(part) if part.isdigit() else part.casefold() for part in _DIGITS.split(path.name)]


def scan(folder: Path) -> tuple[list[Path], dict[str, Path]]:
    """One directory read: the visible media, plus Live Photo videos keyed by their photo's stem.

    The .MOV half of an iPhone Live Photo is hidden from the list: it plays from its photo (L).
    """
    try:
        files = [p for p in folder.iterdir() if p.suffix.lower() in ALL_EXTS and p.is_file()]
    except OSError:
        return [], {}
    photo_stems = {p.stem.casefold() for p in files if p.suffix.lower() in IMAGE_EXTS}
    companions = {p.stem.casefold(): p for p in files
                  if p.suffix.lower() in _MOTION_EXTS and p.stem.casefold() in photo_stems}
    hidden = set(companions.values())
    visible = [p for p in files if not (p.suffix.lower() in VIDEO_EXTS and p in hidden)]
    return sorted(visible, key=natural_key), companions


def list_media(folder: Path) -> list[Path]:
    return scan(folder)[0]


def _folder_stamp(folder: Path) -> int | None:
    """NTFS updates a folder's modification time when files are added, removed or renamed."""
    try:
        return folder.stat().st_mtime_ns
    except OSError:
        return None


class FolderNav:
    """Tracks the current file's position among its folder's media.

    The listing is cached and only re-read when the folder changes, so stepping
    through 5 000 files (or a network share) does not rescan on every key press.
    """

    def __init__(self) -> None:
        self.files: list[Path] = []
        self.index = -1
        self._folder: Path | None = None
        self._stamp: int | None = None
        self._positions: dict[str, int] = {}
        self._companions: dict[str, Path] = {}

    def _rescan(self, folder: Path) -> None:
        self.files, self._companions = scan(folder)
        self._folder, self._stamp = folder, _folder_stamp(folder)
        self._reindex()

    def _reindex(self) -> None:
        self._positions = {str(p).casefold(): i for i, p in enumerate(self.files)}

    def refresh(self) -> None:
        """Force a re-read, for example after this app saved a file into the folder."""
        self._stamp = None

    def set_current(self, path: Path) -> None:
        folder = path.parent
        if folder != self._folder or self._stamp is None or _folder_stamp(folder) != self._stamp:
            self._rescan(folder)
        self.index = self._positions.get(str(path).casefold(), -1)
        if self.index < 0:  # a hidden Live Photo video, or a file created since the last scan
            self._rescan(folder)
            self.index = self._positions.get(str(path).casefold(), -1)
        if self.index < 0:
            self.files.append(path)
            self.files.sort(key=natural_key)
            self._reindex()
            self.index = self._positions[str(path).casefold()]

    def companion_of(self, path: Path) -> Path | None:
        """The Live Photo video for ``path`` (from the cached listing), if it still exists."""
        if path.suffix.lower() not in IMAGE_EXTS:
            return None
        found = self._companions.get(path.stem.casefold())
        return found if found is not None and found.exists() else None

    def step(self, delta: int) -> Path | None:
        if not self.files:
            return None
        i = max(0, min(len(self.files) - 1, self.index + delta))
        return None if i == self.index else self.files[i]

    def first(self) -> Path | None:
        return self.files[0] if self.files and self.index != 0 else None

    def last(self) -> Path | None:
        return self.files[-1] if self.files and self.index != len(self.files) - 1 else None

    def remove(self, path: Path) -> None:
        """Drop ``path`` (deleted or gone) without a rescan."""
        i = self._positions.get(str(path).casefold())
        if i is None:
            return
        self.files.pop(i)
        if self.index > i:
            self.index -= 1
        self.index = min(self.index, len(self.files) - 1)
        self._reindex()
        self._stamp = _folder_stamp(self._folder) if self._folder else None

    def remove_current(self) -> Path | None:
        """Drop the current entry and return the file to show next (or None)."""
        if not 0 <= self.index < len(self.files):
            return None
        self.remove(self.files[self.index])
        if not self.files:
            self.index = -1
            return None
        return self.files[self.index]

    @property
    def position_text(self) -> str:
        return f"{self.index + 1} / {len(self.files)}" if self.files else ""
