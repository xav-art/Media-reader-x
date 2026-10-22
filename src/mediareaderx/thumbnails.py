"""Small, fast previews for the thumbnail strip.

Stills are decoded off the UI thread with format-specific shortcuts (JPEG
draft mode, the JPEG preview embedded in RAW files). Videos are sampled with a
hidden QMediaPlayer on the UI thread, one at a time.
"""

from __future__ import annotations

import gc
import io
import logging
from collections import deque
from pathlib import Path

from PIL import ExifTags, Image, ImageOps
from PySide6.QtCore import QObject, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QImage, QPainter
from PySide6.QtMultimedia import QMediaPlayer, QVideoSink
from PySide6.QtSvg import QSvgRenderer

from mediareaderx import config
from mediareaderx.decoders import (EXIF_TRANSPOSE, JPEG_FORMATS, JXL_THREADS, ORIENTATION_TAG, PSD_EXTS, check_pixels,
                                   jxl_size, ndarray_to_qimage, open_psd, pil_to_qimage, register_plugins)
from mediareaderx.formats import MediaKind, kind_of

log = logging.getLogger(__name__)

THUMB_SIDE = 192
JPEG_EXTS = frozenset({".jpg", ".jpeg", ".jpe", ".jfif"})
EXIF_HEAD_BYTES = 128 * 1024  # the EXIF block (APP1) is at most 64 KB, near the start of the file
JPEG_THUMB_OFFSET, JPEG_THUMB_LENGTH = 0x0201, 0x0202  # IFD1 tags of the embedded preview
PSD_PREVIEW_MIN = 112  # the strip tile's width in logical pixels


def _fit(img: QImage, side: int = THUMB_SIDE) -> QImage:
    return img.scaled(QSize(side, side), Qt.AspectRatioMode.KeepAspectRatio,
                      Qt.TransformationMode.SmoothTransformation)


def _pil_thumb(im: Image.Image, side: int) -> QImage:
    im.draft("RGB", (side, side))  # JPEG: decode at reduced scale, near-free
    im = ImageOps.exif_transpose(im)  # copies, so the caller's context manager can still close it
    im.thumbnail((side, side))
    return pil_to_qimage(im)


def _raw_thumb(path: Path, side: int) -> QImage | None:
    import rawpy

    with rawpy.imread(str(path)) as raw:  # LibRaw is much slower from a memory buffer
        try:
            thumb = raw.extract_thumb()
        except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
            thumb = None
        if thumb is not None and thumb.format == rawpy.ThumbFormat.JPEG:
            with Image.open(io.BytesIO(thumb.data)) as im:
                return _pil_thumb(im, side)
        if thumb is not None and thumb.format == rawpy.ThumbFormat.BITMAP:
            return _fit(ndarray_to_qimage(thumb.data), side)
        return _fit(ndarray_to_qimage(raw.postprocess(half_size=True, output_bps=8)), side)


def _exif_thumbnail(path: Path, side: int) -> QImage | None:
    """The preview a camera or phone stores in the JPEG's EXIF block, if it is at least ``side``.

    EXIF sits at the start of the file, so this reads 128 KB instead of the whole photo and
    decodes a 320 px JPEG instead of a 48 MP one (review: 408 ms -> a few ms per tile).
    Small 160 px previews are skipped: enlarged, they look soft in the strip.
    """
    try:
        with path.open("rb") as f:
            head = f.read(EXIF_HEAD_BYTES)
        with Image.open(io.BytesIO(head)) as im:
            if im.format not in JPEG_FORMATS:
                return None
            check_pixels(*im.size)
            raw, exif = im.info.get("exif"), im.getexif()
        if not raw:
            return None
        ifd1 = exif.get_ifd(ExifTags.IFD.IFD1)
        offset, length = ifd1.get(JPEG_THUMB_OFFSET), ifd1.get(JPEG_THUMB_LENGTH)
        if not offset or not length:
            return None
        tiff = raw[6:] if raw.startswith(b"Exif\x00\x00") else raw  # offsets count from the TIFF header
        data = tiff[offset:offset + length]
        if len(data) != length:
            return None
        with Image.open(io.BytesIO(data)) as thumb:
            if max(thumb.size) < side:
                return None
            thumb.load()
            # The preview is stored as the sensor sees it: turn it like the photo.
            turn = EXIF_TRANSPOSE.get(exif.get(ORIENTATION_TAG, 1))
            return _pil_thumb(thumb.transpose(turn) if turn is not None else thumb, side)
    except Exception:  # no usable preview: the caller decodes the photo itself
        log.debug("No EXIF preview for %s", path.suffix, exc_info=True)
        return None


def _psd_thumb(path: Path, side: int) -> QImage | None:
    """Photoshop's own preview (up to 160 px) when present: sharp at the strip's 112 px tile, and
    it spares flattening a document that can be hundreds of MB. Else the flattened image."""
    psd, _header = open_psd(path)
    preview = psd.thumbnail()
    if preview is None or max(preview.size) < PSD_PREVIEW_MIN:
        preview = psd.topil() if psd.has_preview() else None
    del psd
    gc.collect()  # psd-tools keeps reference cycles: free the parsed file now (see decoders._decode_psd)
    return _pil_thumb(preview, side) if preview is not None else None


def make_thumbnail(path: str | Path, side: int = THUMB_SIDE) -> QImage | None:
    """Return a preview no larger than ``side`` px, or None if it can't be made."""
    register_plugins()
    path = Path(path)
    kind = kind_of(path)
    try:
        if path.stat().st_size > config.MAX_IMAGE_FILE_BYTES or kind in (None, MediaKind.VIDEO):
            return None
        if kind is MediaKind.RAW:
            return _raw_thumb(path, side)
        if kind is MediaKind.SVG:
            renderer = QSvgRenderer(str(path))
            if not renderer.isValid():
                return None
            size = renderer.defaultSize().scaled(side, side, Qt.AspectRatioMode.KeepAspectRatio)
            img = QImage(size if not size.isEmpty() else QSize(side, side), QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(Qt.GlobalColor.transparent)
            painter = QPainter(img)
            renderer.render(painter)
            painter.end()
            return img
        if path.suffix.lower() == ".jxl":
            import imagecodecs

            data = path.read_bytes()
            size = jxl_size(data)
            if size is not None:
                check_pixels(*size)
            arr = imagecodecs.jpegxl_decode(data, numthreads=JXL_THREADS)
            return _fit(ndarray_to_qimage(arr[0] if arr.ndim == 4 else arr), side)
        if path.suffix.lower() in PSD_EXTS:
            return _psd_thumb(path, side)
        if path.suffix.lower() in JPEG_EXTS and (embedded := _exif_thumbnail(path, side)) is not None:
            return embedded
        with Image.open(io.BytesIO(path.read_bytes())) as im:
            check_pixels(*im.size)
            return _pil_thumb(im, side)
    except Exception:  # a bad file must never break the strip
        log.debug("Thumbnail failed for %s", path.suffix, exc_info=True)
        return None


class VideoThumbnailer(QObject):
    """Grabs one frame per video, sequentially, using a hidden muted player."""

    ready = Signal(str, QImage, int)  # path, frame, duration in ms (0 if unknown)
    failed = Signal(str)  # no frame within the timeout, or the video cannot be read
    TIMEOUT_MS = 4000

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue: deque[str] = deque()
        self._current: str | None = None
        self._player = QMediaPlayer(self)  # no audio output attached: silent
        self._sink = QVideoSink(self)
        self._player.setVideoSink(self._sink)
        self._sink.videoFrameChanged.connect(self._on_frame)
        self._player.mediaStatusChanged.connect(self._on_status)
        self._player.errorOccurred.connect(lambda *_: self._finish(None))
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self._finish(None))

    def request(self, path: str) -> None:
        if path != self._current and path not in self._queue:
            self._queue.append(path)
        if self._current is None:
            self._next()

    def clear(self) -> None:
        self._queue.clear()

    @property
    def current(self) -> str | None:
        return self._current

    def _next(self) -> None:
        if not self._queue:
            self._current = None
            return
        self._current = self._queue.popleft()
        self._player.setSource(QUrl.fromLocalFile(self._current))
        self._timer.start(self.TIMEOUT_MS)

    def _on_status(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.LoadedMedia and self._current:
            # Skip a second in (or 10%) to avoid black fade-in frames.
            self._player.setPosition(min(1000, self._player.duration() // 10))
            self._player.play()
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self._finish(None)

    def _on_frame(self, frame) -> None:
        if self._current and frame.isValid():
            img = frame.toImage()
            if not img.isNull():
                self._finish(_fit(img))

    def _finish(self, img: QImage | None) -> None:
        if self._current is None:
            return
        self._timer.stop()
        path, self._current = self._current, None
        duration = max(0, self._player.duration())
        self._player.stop()
        self._player.setSource(QUrl())
        if img is not None:
            self.ready.emit(path, img, duration)
        else:
            self.failed.emit(path)
        QTimer.singleShot(0, self._next)
