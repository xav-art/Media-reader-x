"""What a decode returns, and the checks that need no decoder.

Kept apart from decoders.py so that the app's own process, which no longer decodes anything (see
sandbox.py), never loads numpy, Pillow or the codec libraries: about 15 MB less memory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtGui import QImage

from mediareaderx import config
from mediareaderx.formats import MediaKind, kind_of
from mediareaderx.metadata import MetaItem


class DecodeError(Exception):
    """Raised with a user-facing message when a file cannot be shown."""


@dataclass
class DecodedImage:
    frames: list[QImage]
    durations_ms: list[int]
    format_name: str
    metadata: list[MetaItem] = field(default_factory=list)
    exif_bytes: bytes | None = None  # original EXIF, reused by Save as
    full_size: QSize | None = None  # the picture's real size when the frames are a smaller screen copy
    notice: str | None = None  # set when the content is another format than the extension says

    @property
    def size(self) -> QSize:
        """The picture's real size (upright), even when the frames are a screen copy."""
        return self.full_size if self.full_size is not None else self.frames[0].size()

    @property
    def reduced(self) -> bool:
        return self.full_size is not None and self.full_size != self.frames[0].size()

    @property
    def animated(self) -> bool:
        return len(self.frames) > 1


def image_kind(ext: str) -> MediaKind:
    """The image kind for a file named ``*{ext}``. Raises DecodeError for videos and unknown types."""
    kind = kind_of(Path("file" + ext))
    if kind is None or kind is MediaKind.VIDEO:
        raise DecodeError(f"'{ext}' is not a supported image type.")
    return kind


def check_file_size(size: int) -> None:
    if size > config.MAX_IMAGE_FILE_BYTES:
        raise DecodeError("The file is larger than the 1 GB safety limit for images.")
    if size == 0:
        raise DecodeError("The file is empty.")
