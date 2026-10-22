"""Print several pictures on one A4 page, or save the pages as a PDF.

The page geometry is pure maths in millimetres scaled to device pixels, so the
on-screen preview, the printer and the PDF writer draw the same page, and the
layout is tested without a printer. Pictures are decoded one page at a time
and scaled to their cell, so a page of 50 MP RAW files never holds 9 full decodes.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QMarginsF, QRectF, QSizeF, Qt, QThread
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QImage, QPageLayout, QPageSize, QPagedPaintDevice, QPainter,
                           QTransform)

from mediareaderx import APP_NAME
from mediareaderx.decoders import DecodeError, decode

log = logging.getLogger(__name__)

PER_PAGE = (1, 2, 4, 6, 9)  # 0 means Auto
# Portrait grids as (columns, rows). Landscape swaps them, so cells stay close to the photo shape.
_GRIDS = {1: (1, 1), 2: (1, 2), 4: (2, 2), 6: (2, 3), 9: (3, 3)}
A4_MM = QSizeF(210, 297)
GAP_MM = 4.0
CAPTION_MM = 6.0
CAPTION_FONT_MM = 3.0
MAX_MARGIN_MM = 30
PDF_DPI = 300
PAPER = QColor("white")
CAPTION_INK = QColor(60, 60, 60)
PLACEHOLDER = QColor(225, 228, 234)
NOTE_INK = QColor(110, 115, 125)
MM_PER_INCH = 25.4


@dataclass
class PrintOptions:
    per_page: int = 0  # 0 = Auto: the smallest grid that holds the selection
    landscape: bool = False
    fill: bool = False  # False = Fit (whole picture), True = Fill (crop to the cell)
    margin_mm: float = 10.0
    captions: bool = False
    use_rotation: bool = True


@dataclass
class Cell:
    """One picture on a page: its pixels, or why there are none."""

    name: str
    image: QImage | None = None
    note: str = ""  # shown in place of the picture: "Loading", or why it cannot be printed


def per_page_for(count: int, choice: int) -> int:
    """The grid size to use: the user's choice, or for Auto the smallest grid that holds ``count``."""
    if choice in PER_PAGE:
        return choice
    return next((n for n in PER_PAGE if n >= count), PER_PAGE[-1])


def grid_for(per_page: int, landscape: bool) -> tuple[int, int]:
    cols, rows = _GRIDS.get(per_page, (1, 1))
    return (rows, cols) if landscape else (cols, rows)


def paginate(items: Sequence, per_page: int) -> list[list]:
    per_page = max(1, per_page)
    return [list(items[i:i + per_page]) for i in range(0, len(items), per_page)]


def page_count(count: int, options: PrintOptions) -> int:
    return -(-count // per_page_for(count, options.per_page)) if count else 0


def paper_mm(landscape: bool) -> QSizeF:
    return A4_MM.transposed() if landscape else A4_MM


def cell_rects(area: QRectF, cols: int, rows: int, gap: float) -> list[QRectF]:
    """Equal cells, left to right and top to bottom, ``gap`` apart inside ``area``."""
    w = max(0.0, (area.width() - gap * (cols - 1)) / cols)
    h = max(0.0, (area.height() - gap * (rows - 1)) / rows)
    return [QRectF(area.left() + c * (w + gap), area.top() + r * (h + gap), w, h)
            for r in range(rows) for c in range(cols)]


def fit_rect(image_size: QSizeF, box: QRectF) -> QRectF:
    """The largest rectangle with the picture's shape, centred in ``box``."""
    if image_size.isEmpty() or box.isEmpty():
        return QRectF()
    size = image_size.scaled(box.size(), Qt.AspectRatioMode.KeepAspectRatio)
    return QRectF(box.center().x() - size.width() / 2, box.center().y() - size.height() / 2,
                  size.width(), size.height())


def crop_rect(image_size: QSizeF, box: QRectF) -> QRectF:
    """The centred part of the picture (in its own pixels) that has the shape of ``box``."""
    if image_size.isEmpty() or box.isEmpty():
        return QRectF()
    size = box.size().scaled(image_size, Qt.AspectRatioMode.KeepAspectRatio)
    return QRectF((image_size.width() - size.width()) / 2, (image_size.height() - size.height()) / 2,
                  size.width(), size.height())


def picture_box(cell: QRectF, px_per_mm: float, captions: bool) -> QRectF:
    """The part of a cell for the picture: a caption takes a strip at the bottom."""
    if not captions:
        return cell
    return cell.adjusted(0, 0, 0, -min(cell.height() / 2, CAPTION_MM * px_per_mm))


def render_page(painter: QPainter, paper: QRectF, px_per_mm: float, cells: Sequence[Cell],
                options: PrintOptions, per_page: int, placeholders: bool = True) -> None:
    """Draw one page. ``paper`` is the whole sheet in device pixels; margins are applied here.

    ``placeholders`` draws a grey box for a cell without pixels (the preview). On paper it stays empty.
    """
    margin = min(max(0.0, options.margin_mm), MAX_MARGIN_MM) * px_per_mm
    area = paper.adjusted(margin, margin, -margin, -margin)
    cols, rows = grid_for(per_page, options.landscape)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    font = QFont()
    font.setPixelSize(max(1, round(CAPTION_FONT_MM * px_per_mm)))
    font.setStyleStrategy(QFont.StyleStrategy.NoSubpixelAntialias)  # paper has no RGB subpixels
    painter.setFont(font)
    metrics = QFontMetricsF(font, painter.device())  # the printer's metrics, not the screen's
    for cell, rect in zip(cells, cell_rects(area, cols, rows, GAP_MM * px_per_mm), strict=False):
        box = picture_box(rect, px_per_mm, options.captions)
        img = cell.image
        drawn = box
        if img is not None and not img.isNull():
            size = QSizeF(img.size())
            if options.fill:
                painter.drawImage(box, img, crop_rect(size, box))
            else:
                drawn = fit_rect(size, box)
                painter.drawImage(drawn, img)
        elif placeholders:
            painter.fillRect(box, PLACEHOLDER)
            if cell.note:
                painter.setPen(NOTE_INK)
                painter.drawText(box, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, cell.note)
        if options.captions:  # directly under the picture, not at the foot of the space
            caption = QRectF(rect.left(), drawn.bottom(), rect.width(), rect.bottom() - box.bottom())
            painter.setPen(CAPTION_INK)
            text = metrics.elidedText(cell.name, Qt.TextElideMode.ElideMiddle, caption.width())
            painter.drawText(caption, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, text)
    painter.restore()


# -- output: printer or PDF -------------------------------------------------------------
def load_for_print(path: Path, rotation: int, max_size: QSizeF, fill: bool) -> Cell:
    """Decode ``path`` and shrink it to the cell's device pixels. Runs on a worker thread."""
    try:
        # Decode near the cell size (JPEG and RAW never exist at full size). Fill crops the picture,
        # so it may need a longer side than the cell: twice the cell covers shapes up to 2:1 apart
        # (a 3:2 photo in a square cell needs 1.5x).
        side = math.ceil(max(max_size.width(), max_size.height()) * (2 if fill else 1))
        img = decode(path, side if side > 0 else None).frames[0]
        rotation %= 360
        # Scale first, then rotate: rotating a 50 MP frame would make a second full-size copy.
        target = (max_size.transposed() if rotation in (90, 270) else max_size).toSize()
        mode = Qt.AspectRatioMode.KeepAspectRatioByExpanding if fill else Qt.AspectRatioMode.KeepAspectRatio
        if target.width() > 0 and target.height() > 0 and (
                img.width() > target.width() or img.height() > target.height()):
            scaled = img.size().scaled(target, mode)
            if scaled.width() < img.width():  # never enlarge: the printer scales up smoothly
                img = img.scaled(scaled, Qt.AspectRatioMode.IgnoreAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        if rotation:
            img = img.transformed(QTransform().rotate(rotation))
        return Cell(path.name, img)
    except DecodeError as exc:
        log.warning("Print: decode failed: %s", exc)
    except Exception:  # never let one bad file stop the whole print job
        log.exception("Print: unexpected decode failure")
    return Cell(path.name, None, f"{APP_NAME} cannot print this file.")


def prepare_device(device: QPagedPaintDevice, landscape: bool) -> None:
    device.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    device.setPageOrientation(QPageLayout.Orientation.Landscape if landscape else QPageLayout.Orientation.Portrait)


def _min_margin_mm(device: QPagedPaintDevice) -> float:
    """The printer's hardware margin: the part of the sheet it cannot print on."""
    layout = QPageLayout(device.pageLayout())
    layout.setUnits(QPageLayout.Unit.Millimeter)
    m: QMarginsF = layout.minimumMargins()
    return max(m.left(), m.top(), m.right(), m.bottom(), 0.0)


def render_document(painter: QPainter, device: QPagedPaintDevice, paths: Sequence[Path], options: PrintOptions,
                    rotations: dict[Path, int] | None = None,
                    progress: Callable[[int, int], bool] | None = None) -> tuple[int, list[str]]:
    """Draw every page on an active ``painter`` for ``device``.

    The device draws the whole sheet (QPrinter full-page mode, or a PDF with zero margins).
    ``progress(done, total)`` returns False to cancel. Returns (pages drawn, names that failed).
    """
    rotations = rotations if options.use_rotation and rotations else {}
    per_page = per_page_for(len(paths), options.per_page)
    px_per_mm = device.resolution() / MM_PER_INCH
    full = device.pageLayout().fullRectPixels(device.resolution())
    paper = QRectF(0, 0, full.width(), full.height())
    # Keep the pictures out of the strip the printer cannot reach. The grid follows the sheet, so a
    # change of orientation in the Windows print dialog still gives cells the shape of the paper.
    options = PrintOptions(**{**vars(options), "margin_mm": max(options.margin_mm, _min_margin_mm(device)),
                              "landscape": full.width() > full.height()})
    margin = min(options.margin_mm, MAX_MARGIN_MM) * px_per_mm
    cols, rows = grid_for(per_page, options.landscape)
    cell = cell_rects(paper.adjusted(margin, margin, -margin, -margin), cols, rows, GAP_MM * px_per_mm)[0]
    max_size = picture_box(cell, px_per_mm, options.captions).size()

    failed: list[str] = []
    pages = paginate(list(paths), per_page)
    done = 0
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        for number, page in enumerate(pages):
            if number:
                device.newPage()
            futures = [pool.submit(load_for_print, p, rotations.get(p, 0), max_size, options.fill) for p in page]
            cells: list[Cell] = []
            for future in futures:
                while not future.done():  # keep the progress dialog and its Cancel button alive
                    QCoreApplication.processEvents()
                    QThread.msleep(15)
                    if progress is not None and not progress(done, len(paths)):
                        return number, failed
                cells.append(future.result())
                done += 1
                if progress is not None and not progress(done, len(paths)):
                    return number, failed
            failed += [c.name for c in cells if c.image is None]
            render_page(painter, paper, px_per_mm, cells, options, per_page, placeholders=False)
    finally:
        # Cancel returns at once: queued decodes are dropped, running ones finish on their own.
        pool.shutdown(wait=False, cancel_futures=True)
    return len(pages), failed
