"""Zoomable, pannable image canvas that also plays animated frames."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPainter, QPixmap, QTransform, QWheelEvent
from PySide6.QtWidgets import QFrame, QGraphicsPixmapItem, QGraphicsScene, QGraphicsView

from mediareaderx.decoders import DecodedImage

MIN_ZOOM, MAX_ZOOM = 0.02, 32.0
# Opaque RGBA images (many PNGs) are otherwise copied into a new RGB32 pixmap: +183 MB at 48 MP.
NO_COPY = Qt.ImageConversionFlag.NoOpaqueDetection
# Enlarging the screen copy by up to 25% stays sharp enough; past that, decode full resolution.
# At 1.01 the 4th wheel step already cost 0.6 s CPU and +183 MB on a 48 MP photo (review).
DETAIL_TOLERANCE = 1.25


class ImageView(QGraphicsView):
    """Scene units are the picture's real pixels. A screen copy (``DecodedImage.reduced``) is
    drawn scaled up to that size, so zoom levels, "100%" and rotation mean the same for both.
    ``detailNeeded`` asks for full resolution when the zoom would enlarge the screen copy."""

    zoomChanged = Signal(float)
    detailNeeded = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self._item = QGraphicsPixmapItem()
        self._item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self.scene().addItem(self._item)
        self.setRenderHints(QPainter.RenderHint.SmoothPixmapTransform | QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAccessibleName("Image canvas")
        self._fit = True
        self._image: DecodedImage | None = None
        self._detail_asked = False
        self._frame = 0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._next_frame)

    # -- content ---------------------------------------------------------------
    def set_image(self, image: DecodedImage) -> None:
        self._timer.stop()
        self._image = image
        self._detail_asked = False
        self._frame = 0
        self._item.setRotation(0)
        self._show_frame(image)
        self.fit_to_window()
        if image.animated:
            self._timer.start(image.durations_ms[0])

    def _show_frame(self, image: DecodedImage) -> None:
        frame = image.frames[0]
        self._item.setPixmap(QPixmap.fromImage(frame, NO_COPY))
        self._item.setScale(image.size.width() / frame.width() if image.reduced else 1.0)
        self._item.setTransformOriginPoint(self._item.boundingRect().center())
        self.scene().setSceneRect(self._item.sceneBoundingRect())

    def upgrade(self, image: DecodedImage) -> None:
        """Swap the screen copy for full resolution. Zoom, position and rotation stay as they are."""
        if self._image is None or not self._image.reduced or image.size != self._image.size:
            return
        self._image = image
        self._show_frame(image)

    @property
    def reduced(self) -> bool:
        return self._image is not None and self._image.reduced

    def clear(self) -> None:
        self._timer.stop()
        self._image = None
        self._item.setPixmap(QPixmap())
        self._item.setRotation(0)
        self._item.setScale(1.0)

    # -- rotation (view only; the file is never modified) -----------------------
    @property
    def rotation(self) -> int:
        return int(self._item.rotation()) % 360

    def rotate_by(self, degrees: int) -> None:
        if self._item.pixmap().isNull():
            return
        self._item.setRotation((self.rotation + degrees) % 360)
        self.scene().setSceneRect(self._item.sceneBoundingRect())
        if self._fit:
            self.fit_to_window()

    def current_frame(self) -> QImage | None:
        """The frame on screen, rotated as displayed."""
        if not self._image:
            return None
        frame = self._image.frames[self._frame]
        return frame.transformed(QTransform().rotate(self.rotation)) if self.rotation else frame

    def _next_frame(self) -> None:
        if not self._image:
            return
        self._frame = (self._frame + 1) % len(self._image.frames)
        self._item.setPixmap(QPixmap.fromImage(self._image.frames[self._frame], NO_COPY))
        self._timer.start(self._image.durations_ms[self._frame])

    # -- zoom ------------------------------------------------------------------
    @property
    def zoom(self) -> float:
        return self.transform().m11()

    def fit_to_window(self) -> None:
        self._fit = True
        if self._item.pixmap().isNull():
            return
        self.resetTransform()
        rect = self._item.sceneBoundingRect()
        view = self.viewport().rect()
        if rect.width() > view.width() or rect.height() > view.height():
            self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)
        self._zoom_changed()

    def actual_size(self) -> None:
        self._fit = False
        self.resetTransform()
        self._zoom_changed()

    def zoom_by(self, factor: float) -> None:
        target = min(MAX_ZOOM, max(MIN_ZOOM, self.zoom * factor))
        self._fit = False
        self.scale(target / self.zoom, target / self.zoom)
        self._zoom_changed()

    def _zoom_changed(self) -> None:
        self.zoomChanged.emit(self.zoom)
        if self.reduced and not self._detail_asked:
            frame = self._image.frames[0]
            # Device pixels per real pixel, against what the screen copy holds per real pixel.
            if self.zoom * self.devicePixelRatioF() > frame.width() / self._image.size.width() * DETAIL_TOLERANCE:
                self._detail_asked = True
                self.detailNeeded.emit()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self._item.pixmap().isNull():
            return
        self.zoom_by(1.25 if event.angleDelta().y() > 0 else 0.8)

    def mouseDoubleClickEvent(self, event) -> None:
        if self._fit:
            self.actual_size()
        else:
            self.fit_to_window()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._fit:
            self.fit_to_window()
