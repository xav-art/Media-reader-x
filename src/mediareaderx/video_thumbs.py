"""Video thumbnails for the strip, made by a protected helper (see sandbox.py).

Same interface as ``thumbnails.VideoThumbnailer`` (request, clear, release, current, ready, failed), so the
strip does not care which one runs. One video at a time, on a worker thread. When protected decoding is
off or has never worked on this PC, it hands its queue to the in-process ``VideoThumbnailer``.
"""

from __future__ import annotations

import logging
import threading
from collections import deque

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from mediareaderx import sandbox

log = logging.getLogger(__name__)

_IN_PROCESS = "in-process"


class ProtectedVideoThumbnailer(QObject):
    ready = Signal(str, QImage, int)  # path, frame, duration in ms (0 if unknown)
    failed = Signal(str)
    _done = Signal(str, object, int)  # from the worker thread, delivered on the GUI thread

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue: deque[str] = deque()
        self._current: str | None = None
        self._fallback = None
        self._done.connect(self._on_done)

    @property
    def current(self) -> str | None:
        return self._fallback.current if self._fallback is not None else self._current

    def request(self, path: str) -> None:
        if self._fallback is not None:
            self._fallback.request(path)
            return
        if path != self._current and path not in self._queue:
            self._queue.append(path)
        if self._current is None:
            self._next()

    def clear(self) -> None:
        self._queue.clear()
        if self._fallback is not None:
            self._fallback.clear()

    def release(self, path: str) -> None:
        """Drop ``path`` from the queue. A running grab may finish: the helper's handle does not stop the move
        to the Recycle Bin (it allows delete), and the strip ignores a thumbnail of a file it no longer lists."""
        try:
            self._queue.remove(path)
        except ValueError:
            pass
        if self._fallback is not None:
            self._fallback.release(path)

    def _next(self) -> None:
        if not self._queue:
            self._current = None
            return
        self._current = self._queue.popleft()
        threading.Thread(target=self._work, args=(self._current,), name="video-thumb", daemon=True).start()

    def _work(self, path: str) -> None:
        try:
            img, duration = sandbox.video_thumbnail(path)
        except sandbox.UseInProcess:
            img, duration = _IN_PROCESS, 0
        except Exception:  # never leave the queue stuck
            log.debug("Video thumbnail failed", exc_info=True)
            img, duration = None, 0
        try:
            self._done.emit(path, img, duration)
        except RuntimeError:
            pass  # the strip was destroyed meanwhile

    def _on_done(self, path: str, img, duration: int) -> None:
        if img == _IN_PROCESS:
            from mediareaderx.thumbnails import VideoThumbnailer

            self._fallback = VideoThumbnailer(self)
            self._fallback.ready.connect(self.ready)
            self._fallback.failed.connect(self.failed)
            pending, self._queue, self._current = [path, *self._queue], deque(), None
            for item in pending:
                self._fallback.request(item)
            return
        self._current = None
        if isinstance(img, QImage) and not img.isNull():
            self.ready.emit(path, img, duration)
        else:
            self.failed.emit(path)
        self._next()
