"""Save the displayed image as JPG or PNG (e.g. to share an iPhone HEIC).

The pixels written are what the viewer shows: sRGB, upright, plus any view
rotation. Original EXIF (date, camera, ...) is kept with orientation reset,
and the GPS block can optionally be removed for privacy.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image
from PySide6.QtGui import QImage, QTransform

from mediareaderx.metadata import EXIF_IFD, GPS_IFD

log = logging.getLogger(__name__)

JPEG_QUALITY = 92
ORIENTATION_TAG = 0x0112
MAKER_NOTE_TAG = 0x927C
JPEG_EXIF_LIMIT = 65533  # the APP1 segment holds at most 64 KB


class ExportError(Exception):
    """Raised with a user-facing message when saving fails."""


def qimage_to_pil(img: QImage) -> Image.Image:
    img = img.convertToFormat(QImage.Format.Format_RGBA8888)
    data = bytes(img.constBits())
    return Image.frombuffer("RGBA", (img.width(), img.height()), data, "raw", "RGBA", img.bytesPerLine(), 1)


def _prepare_exif(exif_bytes: bytes | None, strip_gps: bool) -> bytes | None:
    if not exif_bytes:
        return None
    exif = Image.Exif()
    try:
        exif.load(exif_bytes)
    except Exception:  # malformed EXIF: save the pixels without it
        return None
    exif[ORIENTATION_TAG] = 1  # pixels are already upright
    # The camera's private MakerNote describes the original sensor data, not the new pixels,
    # and it alone can push RAW EXIF past the 64 KB JPEG limit.
    exif.get_ifd(EXIF_IFD).pop(MAKER_NOTE_TAG, None)
    if strip_gps and GPS_IFD in exif:
        del exif[GPS_IFD]
    try:
        return exif.tobytes()
    except Exception:
        log.warning("EXIF could not be rewritten; the copy is saved without it")
        return None


def export_image(frame: QImage, dest: str | Path, *, source: str | Path | None = None,
                 rotation: int = 0, exif_bytes: bytes | None = None, strip_gps: bool = False) -> Path:
    dest = Path(dest)
    if source is not None and dest.resolve() == Path(source).resolve():
        raise ExportError("Choose a different file name. Media Reader x never overwrites the original.")
    fmt = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG"}.get(dest.suffix.lower())
    if fmt is None:
        raise ExportError("Save as .jpg or .png.")
    if rotation % 360:
        frame = frame.transformed(QTransform().rotate(rotation))
    im = qimage_to_pil(frame)
    params: dict = {}
    exif = _prepare_exif(exif_bytes, strip_gps)
    if exif and fmt == "JPEG" and len(exif) > JPEG_EXIF_LIMIT:
        log.warning("EXIF is %d bytes, over the JPEG limit; the copy is saved without it", len(exif))
        exif = None
    if exif:
        params["exif"] = exif
    if fmt == "JPEG":
        if im.getextrema()[3][0] < 255:  # flatten transparency onto white
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.getchannel("A"))
            im = bg
        else:
            im = im.convert("RGB")
        params.update(quality=JPEG_QUALITY, optimize=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        im.save(tmp, fmt, **params)
        tmp.replace(dest)  # atomic: never leaves a half-written file
    except (OSError, ValueError) as exc:
        tmp.unlink(missing_ok=True)
        log.exception("Save a copy failed")
        raise ExportError(f"Media Reader x did not save the copy: {getattr(exc, 'strerror', None) or exc}") from exc
    return dest
