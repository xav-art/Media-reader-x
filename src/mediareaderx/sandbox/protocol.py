"""Messages between the app and a helper: a 4-byte length, UTF-8 JSON, then raw blobs the JSON declares.

No pickle, no code, no paths. The app treats every reply as hostile: ``read_image`` and ``read_decoded``
check every field, size and string before anything is used, and read pixels straight into a new QImage.
"""

from __future__ import annotations

import json
import struct
from typing import BinaryIO

MAX_HEADER = 1 << 20
MAX_TEXT = 300
MAX_META_ROWS = 1000
MAX_META_TEXT = 2000
MAX_EXIF = 4 << 20
MAX_SIDE = 65535

# The two pixel formats a reply may use (QImage.Format values).
FORMATS = {"rgb32": 4, "argb32p": 6}  # Format_RGB32, Format_ARGB32_Premultiplied


class ProtocolError(Exception):
    """A message broke the rules. The helper is killed and the file is shown as damaged."""


class HelperGone(Exception):
    """The helper exited or was killed before it replied."""


# -- low level -----------------------------------------------------------------------------------------
def write_message(f: BinaryIO, header: dict, blobs: list = ()) -> None:
    header = dict(header, blobs=[memoryview(b).nbytes for b in blobs])
    data = json.dumps(header, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    if len(data) > MAX_HEADER:
        raise ProtocolError("header too large")
    f.write(struct.pack("<I", len(data)) + data)
    for blob in blobs:
        view = memoryview(blob).cast("B")
        done = 0
        while done < len(view):
            done += f.write(view[done:done + (8 << 20)])
    f.flush()


def read_into(f: BinaryIO, view: memoryview) -> None:
    view = view.cast("B")
    done = 0
    while done < len(view):
        n = f.readinto(view[done:done + (8 << 20)])
        if not n:
            raise HelperGone()
        done += n


def read_exact(f: BinaryIO, size: int) -> bytes:
    buf = bytearray(size)
    read_into(f, memoryview(buf))
    return bytes(buf)


def read_header(f: BinaryIO) -> dict:
    size = struct.unpack("<I", read_exact(f, 4))[0]
    if size > MAX_HEADER:
        raise ProtocolError("header too large")
    try:
        header = json.loads(read_exact(f, size).decode("utf-8"))
    except HelperGone:
        raise
    except Exception as exc:  # bad UTF-8, bad JSON, or JSON nested deep enough to exhaust the stack
        raise ProtocolError("bad header") from exc
    if not isinstance(header, dict):
        raise ProtocolError("bad header")
    blobs = header.get("blobs", [])
    if not isinstance(blobs, list) or not all(_is_int(b, 0, 1 << 34) for b in blobs):
        raise ProtocolError("bad blob list")
    return header


# -- checks ----------------------------------------------------------------------------------------------
def _is_int(value, lo: int, hi: int) -> bool:
    return type(value) is int and lo <= value <= hi


def need_int(header: dict, key: str, lo: int, hi: int) -> int:
    value = header.get(key)
    if not _is_int(value, lo, hi):
        raise ProtocolError(f"bad {key}")
    return value


_HIDDEN = ("Cc", "Cf", "Zl", "Zp", "Cs", "Co")  # controls, format and direction marks, line breaks, surrogates


def clean_text(value, limit: int = MAX_TEXT) -> str:
    """A plain one-line string, at most ``limit`` characters. Controls, invisible format characters and
    direction overrides become spaces (a right-to-left override can make "photo<RLO>gpj.exe" read as
    "photoexe.jpg")."""
    import unicodedata

    if not isinstance(value, str):
        raise ProtocolError("bad text")
    return "".join(" " if unicodedata.category(ch) in _HIDDEN else ch for ch in value[:limit])


def optional_text(header: dict, key: str, limit: int = MAX_TEXT) -> str | None:
    value = header.get(key)
    return None if value is None else clean_text(value, limit)


# -- images ----------------------------------------------------------------------------------------------
def image_parts(img, hold: list) -> tuple[list, memoryview]:
    """Helper side: [width, height, format] and the pixels of a QImage, in one of the two allowed formats.

    The view points into the QImage's own memory and does not keep it alive: the image goes into ``hold``,
    which the caller keeps until the message is written."""
    from PySide6.QtGui import QImage

    if img.format() not in (QImage.Format.Format_RGB32, QImage.Format.Format_ARGB32_Premultiplied):
        has_alpha = img.hasAlphaChannel()
        img = img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied if has_alpha
                                  else QImage.Format.Format_RGB32)
    name = "argb32p" if img.format() == QImage.Format.Format_ARGB32_Premultiplied else "rgb32"
    if img.bytesPerLine() != img.width() * 4:
        img = img.copy()
    hold.append(img)
    return [img.width(), img.height(), name], memoryview(img.constBits()).cast("B")[:img.width() * img.height() * 4]


def read_image(f: BinaryIO, spec, blob_size: int, max_pixels: int):
    """App side: check one frame's description against its blob and read the pixels into a new QImage."""
    from PySide6.QtGui import QImage

    if (not isinstance(spec, list) or len(spec) != 3 or not _is_int(spec[0], 1, MAX_SIDE)
            or not _is_int(spec[1], 1, MAX_SIDE) or not isinstance(spec[2], str) or spec[2] not in FORMATS):
        raise ProtocolError("bad frame")
    width, height, name = spec
    if width * height > max_pixels or blob_size != width * height * 4:
        raise ProtocolError("bad frame size")
    img = QImage(width, height, QImage.Format(FORMATS[name]))
    if img.isNull() or img.bytesPerLine() != width * 4:
        raise MemoryError("no memory for the picture")
    read_into(f, memoryview(img.bits()))
    return img
