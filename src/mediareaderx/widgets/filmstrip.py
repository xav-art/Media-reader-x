"""Horizontal thumbnail strip of the current folder, centred on the open file."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import (QEvent, QItemSelectionModel, QObject, QPointF, QRectF, QRunnable, QSize, Qt, QThreadPool,
                            Signal)
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPixmap, QPolygonF
from PySide6.QtWidgets import QAbstractItemView, QDockWidget, QListView, QListWidget, QListWidgetItem, QWidget

from mediareaderx.formats import MediaKind, kind_of
from mediareaderx.theme import CORAL
from mediareaderx.thumbnails import VideoThumbnailer, make_thumbnail
from mediareaderx.widgets.video_view import fmt_ms

ICON = QSize(112, 84)
GRID = QSize(124, 96)
TILE_SCALE = 2  # tiles are drawn at 2x so badges stay sharp at 150-200% display scaling
CACHE_LIMIT = 300  # finished tiles kept in memory (about 150 KB each at 2x)
PATH_ROLE = Qt.ItemDataRole.UserRole
SCROLLBAR_PX = 8
# The open file gets a coral ring (brand accent); a thin scrollbar keeps the strip quiet.
STRIP_STYLE = f"""
QListWidget {{ border: none; }}
QListWidget::item {{ border: 3px solid transparent; border-radius: 8px; padding: 2px; }}
QListWidget::item:selected {{ border: 3px solid {CORAL}; background: transparent; }}
QListWidget::item:hover:!selected {{ border: 3px solid palette(mid); }}
QScrollBar:horizontal {{ height: {SCROLLBAR_PX}px; background: transparent; margin: 0; }}
QScrollBar::handle:horizontal {{ background: palette(mid); border-radius: 4px; min-width: 48px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: none; }}
"""

_EXT_LABELS = {"jpeg": "JPG", "jpe": "JPG", "jfif": "JPG", "tiff": "TIF", "hif": "HEIF", "svgz": "SVG"}


def format_label(path: Path, duration_ms: int = 0) -> str:
    """Badge text: RAW for camera files, the format otherwise, plus video length when known."""
    if kind_of(path) is MediaKind.RAW:
        return "RAW"
    ext = path.suffix.lower().lstrip(".")
    label = _EXT_LABELS.get(ext, ext.upper())
    if kind_of(path) is MediaKind.VIDEO and duration_ms > 0:
        return f"{label}  {fmt_ms(duration_ms)}"
    return label


class _ThumbSignals(QObject):
    done = Signal(str, object)  # path, QImage or None when no thumbnail can be made


class _ThumbTask(QRunnable):
    def __init__(self, path: str, signals: _ThumbSignals):
        super().__init__()
        self.path, self.signals = path, signals
        self.started = False

    def run(self) -> None:
        self.started = True
        try:
            img = make_thumbnail(self.path)
        except Exception:  # make_thumbnail already guards; never let a worker exception escape
            img = None
        try:
            self.signals.done.emit(self.path, img)
        except RuntimeError:
            pass  # the strip was destroyed while this thumbnail decoded


def compose_tile(img: QImage | None, label: str, video: bool) -> QIcon:
    """Draw the thumbnail (or a placeholder) with its format badge and, for video, a play mark."""
    w, h = ICON.width() * TILE_SCALE, ICON.height() * TILE_SCALE
    tile = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    tile.fill(Qt.GlobalColor.transparent)
    p = QPainter(tile)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    if img is not None and not img.isNull():
        scaled = img.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        area = QRectF((w - scaled.width()) / 2, (h - scaled.height()) / 2, scaled.width(), scaled.height())
        p.drawImage(area, scaled)
    else:
        area = QRectF(0, 0, w, h)
        p.fillRect(area, QColor(128, 128, 128, 60))
    if video:
        r = max(10.0, min(area.width(), area.height()) / 5)
        c = area.center()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 150))
        p.drawEllipse(c, r, r)
        p.setBrush(QColor(255, 255, 255, 230))
        p.drawPolygon(QPolygonF([QPointF(c.x() - r * 0.35, c.y() - r * 0.5), QPointF(c.x() - r * 0.35, c.y() + r * 0.5),
                                 QPointF(c.x() + r * 0.55, c.y())]))
    # Badge: white text on a dark pill, bottom-left of the picture (>= 4.5:1 on any photo).
    font = QFont()
    font.setPixelSize(10 * TILE_SCALE)
    font.setBold(True)
    p.setFont(font)
    metrics = p.fontMetrics()
    pad = 4 * TILE_SCALE
    pill = QRectF(area.left() + pad, area.bottom() - pad - metrics.height() - 2 * TILE_SCALE,
                  metrics.horizontalAdvance(label) + 2 * pad, metrics.height() + 2 * TILE_SCALE)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(12, 17, 32, 200))
    p.drawRoundedRect(pill, 4 * TILE_SCALE, 4 * TILE_SCALE)
    p.setPen(QColor("white"))
    p.drawText(pill, Qt.AlignmentFlag.AlignCenter, label)
    p.end()
    pixmap = QPixmap.fromImage(tile)
    pixmap.setDevicePixelRatio(TILE_SCALE)
    icon = QIcon(pixmap)
    icon.addPixmap(pixmap, QIcon.Mode.Selected)  # the coral ring marks selection; keep the photo untinted
    return icon


class Filmstrip(QDockWidget):
    fileActivated = Signal(Path)

    def __init__(self, parent=None):
        super().__init__("Thumbnails", parent)
        self.setObjectName("Filmstrip")
        self.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable)
        self.setTitleBarWidget(QWidget(self))  # no title bar: T or View > Thumbnail strip toggles it
        self.list = QListWidget(self)
        self.list.setStyleSheet(STRIP_STYLE)
        self.list.setViewMode(QListView.ViewMode.IconMode)
        self.list.setFlow(QListView.Flow.LeftToRight)
        self.list.setWrapping(False)
        self.list.setMovement(QListView.Movement.Static)
        self.list.setResizeMode(QListView.ResizeMode.Fixed)
        self.list.setIconSize(ICON)
        self.list.setGridSize(GRID)
        self.list.setUniformItemSizes(True)
        self.list.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # keep arrow keys for navigation
        self.list.setFixedHeight(GRID.height() + SCROLLBAR_PX + 6)
        self.list.setAccessibleName("Thumbnails of files in this folder")
        # Ctrl+click and Shift+click select several files (for Print); a plain click opens the file.
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.itemClicked.connect(self._on_clicked)
        self._press_modifiers = Qt.KeyboardModifier.NoModifier
        self.list.viewport().installEventFilter(self)
        self.list.horizontalScrollBar().valueChanged.connect(lambda _v: self._request_visible())
        self.setWidget(self.list)

        self._files: list[Path] = []
        self._rows: dict[str, int] = {}  # path -> row, so storing a thumbnail is O(1)
        self._cache: OrderedDict[str, QIcon] = OrderedDict()
        self._tasks: dict[str, _ThumbTask] = {}  # queued or running image thumbnails
        self._video_pending: set[str] = set()
        self._failed: set[str] = set()  # no thumbnail possible: do not retry
        # One shared placeholder per (badge, video): 5 000 files cost a few tiles, not 731 MB.
        self._placeholders: dict[tuple[str, bool], QIcon] = {}
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._signals = _ThumbSignals(self)
        self._signals.done.connect(self._on_thumb)
        self._video = VideoThumbnailer(self)
        self._video.ready.connect(self._on_video_thumb)
        self._video.failed.connect(self._video_pending.discard)

    # -- public API ------------------------------------------------------------
    def _placeholder(self, label: str, video: bool) -> QIcon:
        key = (label, video)
        if key not in self._placeholders:
            self._placeholders[key] = compose_tile(None, label, video)
        return self._placeholders[key]

    def set_files(self, files: list[Path], current: int) -> None:
        if files != self._files:
            self._files = list(files)
            self._rows = {str(p): i for i, p in enumerate(self._files)}
            self._cancel_offscreen()
            self.list.clear()
            for path in self._files:
                key = str(path)
                is_video = kind_of(path) is MediaKind.VIDEO
                label = format_label(path)
                icon = self._cache.get(key) or self._placeholder(label, is_video)
                item = QListWidgetItem(icon, "")
                item.setData(PATH_ROLE, key)
                item.setToolTip(path.name)
                item.setData(Qt.ItemDataRole.AccessibleTextRole, f"{path.name}, {label}")
                item.setSizeHint(GRID)
                self.list.addItem(item)
        if 0 <= current < self.list.count():
            item = self.list.item(current)
            # Opening a file resets a multi-select, whatever keys are down.
            self.list.setCurrentItem(item, QItemSelectionModel.SelectionFlag.ClearAndSelect)
            self.list.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtCenter)
        self._request_visible()

    def eventFilter(self, obj, event) -> bool:
        # Qt selects on press, so decide on the keys held at press: releasing Ctrl before the
        # button must not turn a Ctrl+click into an open.
        if obj is self.list.viewport() and event.type() == QEvent.Type.MouseButtonPress:
            self._press_modifiers = event.modifiers()
        return super().eventFilter(obj, event)

    def _on_clicked(self, item: QListWidgetItem) -> None:
        if not self._press_modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
            self.fileActivated.emit(Path(item.data(PATH_ROLE)))

    def selected_files(self) -> list[Path]:
        """The selected files in folder order (the open file is selected unless Ctrl+click removed it)."""
        rows = sorted(self.list.row(item) for item in self.list.selectedItems())
        return [self._files[r] for r in rows if 0 <= r < len(self._files)]

    def invalidate(self, path: Path) -> None:
        self._cache.pop(str(path), None)
        self._failed.discard(str(path))

    # -- thumbnail loading -------------------------------------------------------
    def _cancel_offscreen(self) -> None:
        """Forget queued work. Thumbnails already being made finish and are kept."""
        self._pool.clear()
        self._tasks = {p: t for p, t in self._tasks.items() if t.started}
        self._video.clear()
        self._video_pending = {p for p in self._video_pending if p == self._video.current}

    def _request_visible(self) -> None:
        if not self._files or not self.isVisible():
            return
        width = max(1, GRID.width())
        start = self.list.horizontalScrollBar().value() // width
        span = self.list.viewport().width() // width + 1
        # Visible tiles first, then a margin either side for smooth scrolling. Work queued for
        # tiles the user scrolled past is dropped, so a fast scroll never builds a long queue.
        lo, hi = max(0, start - span), min(len(self._files), start + 2 * span + 1)
        self._cancel_offscreen()
        order = sorted(range(lo, hi), key=lambda i: (not start <= i <= start + span, abs(i - start)))
        for i in order:
            key = str(self._files[i])
            if key in self._cache or key in self._tasks or key in self._video_pending or key in self._failed:
                continue
            if kind_of(key) is MediaKind.VIDEO:
                self._video_pending.add(key)
                self._video.request(key)
            else:
                task = _ThumbTask(key, self._signals)
                self._tasks[key] = task
                self._pool.start(task)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._request_visible()

    def _item(self, path: str) -> QListWidgetItem | None:
        row = self._rows.get(path)
        return self.list.item(row) if row is not None else None

    def _store(self, path: str, icon: QIcon) -> None:
        self._cache[path] = icon
        self._cache.move_to_end(path)
        while len(self._cache) > CACHE_LIMIT:
            old, _icon = self._cache.popitem(last=False)
            item = self._item(old)
            if item is not None:  # give the memory back: the item showed the evicted icon
                p = Path(old)
                item.setIcon(self._placeholder(format_label(p), kind_of(p) is MediaKind.VIDEO))
        item = self._item(path)
        if item is not None:
            item.setIcon(icon)

    def _on_thumb(self, path: str, img: QImage | None) -> None:
        self._tasks.pop(path, None)
        if img is None:
            self._failed.add(path)
            return
        self._store(path, compose_tile(img, format_label(Path(path)), video=False))

    def _on_video_thumb(self, path: str, img: QImage, duration_ms: int) -> None:
        self._video_pending.discard(path)
        label = format_label(Path(path), duration_ms)
        item = self._item(path)
        if item is not None:
            item.setData(Qt.ItemDataRole.AccessibleTextRole, f"{Path(path).name}, {label}")
        self._store(path, compose_tile(img, label, video=True))

    def shutdown(self) -> None:
        self._video.clear()
        self._pool.clear()
        self._pool.waitForDone(2000)
