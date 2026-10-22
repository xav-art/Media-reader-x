"""Print dialog: choose pictures and a layout, see the A4 page, then print or save a PDF."""

from __future__ import annotations

import logging
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QMarginsF, QObject, QRectF, QRunnable, QSettings, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QImage, QPainter, QPalette, QPdfWriter, QTransform
from PySide6.QtPrintSupport import QAbstractPrintDialog, QPrintDialog, QPrinter
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox, QProgressDialog,
                               QPushButton, QSpinBox, QToolButton, QVBoxLayout, QWidget)

from mediareaderx import APP_NAME
from mediareaderx.logging_setup import describe_file
from mediareaderx.printing import (MAX_MARGIN_MM, PDF_DPI, PER_PAGE, PAPER, Cell, PrintOptions, page_count,
                                   paginate, paper_mm, per_page_for, prepare_device, render_document, render_page)
from mediareaderx.thumbnails import make_thumbnail

log = logging.getLogger(__name__)

PATH_ROLE = Qt.ItemDataRole.UserRole
PREVIEW_SIDE = 900  # enough for a sharp 1-per-page preview on a 4K screen
PREVIEW_CACHE = 48
PAGE_SHADOW = 3
PRINTER_MARGIN_HINT_MM = 5  # office printers measured here: 4.0 and 5.1 mm hardware margins


def read_options(settings: QSettings) -> PrintOptions:
    """The saved print settings. A damaged value falls back to its default, never an error."""
    default = PrintOptions()

    def number(key: str, fallback, valid) -> int:
        try:
            value = int(settings.value(f"print/{key}", fallback))
        except (TypeError, ValueError):
            return fallback
        return value if valid(value) else fallback

    def flag(key: str, fallback: bool) -> bool:
        value = settings.value(f"print/{key}", fallback)
        return value.lower() == "true" if isinstance(value, str) else bool(value)

    return PrintOptions(
        per_page=number("per_page", default.per_page, lambda v: v == 0 or v in PER_PAGE),
        landscape=flag("landscape", default.landscape),
        fill=flag("fill", default.fill),
        margin_mm=number("margin_mm", int(default.margin_mm), lambda v: 0 <= v <= MAX_MARGIN_MM),
        captions=flag("captions", default.captions),
        use_rotation=flag("use_rotation", default.use_rotation))


def write_options(settings: QSettings, options: PrintOptions) -> None:
    for key, value in vars(options).items():
        settings.setValue(f"print/{key}", int(value) if key == "margin_mm" else value)


class _PreviewSignals(QObject):
    done = Signal(str, object)  # path, QImage or None


class _PreviewTask(QRunnable):
    def __init__(self, path: str, signals: _PreviewSignals):
        super().__init__()
        self.path, self.signals = path, signals

    def run(self) -> None:
        try:
            img = make_thumbnail(self.path, PREVIEW_SIDE)
        except Exception:  # make_thumbnail already guards; a worker must always report back
            img = None
        try:
            self.signals.done.emit(self.path, img)
        except RuntimeError:
            pass  # the dialog closed while this preview decoded


_POOL: QThreadPool | None = None


def _preview_pool() -> QThreadPool:
    """One pool for all print previews. It outlives each dialog, so closing one never blocks."""
    global _POOL
    if _POOL is None:
        _POOL = QThreadPool()
        _POOL.setMaxThreadCount(2)
    return _POOL


class PagePreview(QWidget):
    """Draws one sheet of paper, scaled to the widget, with the same code as the printer."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(QSize(280, 360))
        self.setAccessibleName("Page preview")
        self.cells: list[Cell] = []
        self.options = PrintOptions()
        self.per_page = 1

    def show_page(self, cells: list[Cell], options: PrintOptions, per_page: int) -> None:
        self.cells, self.options, self.per_page = cells, options, per_page
        self.update()

    def paper_rect(self) -> QRectF:
        mm = paper_mm(self.options.landscape)
        box = QRectF(self.rect()).adjusted(8, 8, -8 - PAGE_SHADOW, -8 - PAGE_SHADOW)
        size = mm.scaled(box.size(), Qt.AspectRatioMode.KeepAspectRatio)
        return QRectF(box.center().x() - size.width() / 2, box.center().y() - size.height() / 2,
                      size.width(), size.height())

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        paper = self.paper_rect()
        p.fillRect(paper.translated(PAGE_SHADOW, PAGE_SHADOW), self.palette().color(QPalette.ColorRole.Shadow))
        p.fillRect(paper, PAPER)  # paper is white in both themes
        p.translate(paper.topLeft())
        render_page(p, QRectF(0, 0, paper.width(), paper.height()), paper.width() / paper_mm(
            self.options.landscape).width(), self.cells, self.options, self.per_page)
        p.end()


class PrintDialog(QDialog):
    """``files`` are the printable pictures of the folder; ``checked`` start ticked."""

    def __init__(self, files: list[Path], checked: set[Path], rotations: dict[Path, int], settings: QSettings,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Print")
        self.settings = settings
        self.rotations = {p: r for p, r in rotations.items() if r % 360}
        self._page = 0
        self._previews: OrderedDict[str, QImage | None] = OrderedDict()
        self._pending: set[str] = set()
        self._busy = False  # a print or PDF job is drawing
        self._closed = False
        self._progress: QProgressDialog | None = None
        self._pool = _preview_pool()
        # No parent: running tasks keep it alive, so closing the dialog never waits for a slow preview.
        # Qt drops the connection when the dialog is deleted.
        self._signals = _PreviewSignals()
        self._signals.done.connect(self._on_preview)

        # Pictures: a checklist of the folder, in folder order.
        self.list = QListWidget(self)
        self.list.setAccessibleName("Pictures to print")
        for path in files:
            item = QListWidgetItem(path.name)
            item.setData(PATH_ROLE, str(path))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if path in checked else Qt.CheckState.Unchecked)
            item.setToolTip(str(path))
            self.list.addItem(item)
        self.list.itemChanged.connect(lambda _it: self._on_selection_changed())
        select_all = QPushButton("Select all", self)
        select_all.clicked.connect(lambda: self._check_all(True))
        clear_all = QPushButton("Clear all", self)
        clear_all.clicked.connect(lambda: self._check_all(False))
        left = QVBoxLayout()
        left.addWidget(QLabel("Pictures", self))
        left.addWidget(self.list, 1)
        buttons = QHBoxLayout()
        buttons.addWidget(select_all)
        buttons.addWidget(clear_all)
        left.addLayout(buttons)

        # Preview with page navigation.
        self.preview = PagePreview(self)
        self.prev_btn = QToolButton(self)
        self.prev_btn.setArrowType(Qt.ArrowType.LeftArrow)
        self.prev_btn.setToolTip("Previous page")
        self.prev_btn.setAccessibleName("Previous page")
        self.prev_btn.clicked.connect(lambda: self._step_page(-1))
        self.next_btn = QToolButton(self)
        self.next_btn.setArrowType(Qt.ArrowType.RightArrow)
        self.next_btn.setToolTip("Next page")
        self.next_btn.setAccessibleName("Next page")
        self.next_btn.clicked.connect(lambda: self._step_page(1))
        self.page_label = QLabel(self)
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        nav = QHBoxLayout()
        nav.addStretch(1)
        nav.addWidget(self.prev_btn)
        nav.addWidget(self.page_label)
        nav.addWidget(self.next_btn)
        nav.addStretch(1)
        middle = QVBoxLayout()
        middle.addWidget(self.preview, 1)
        middle.addLayout(nav)

        # Options.
        options = read_options(settings)
        self.layout_box = QComboBox(self)
        self.layout_box.addItem("Auto", 0)
        for n in PER_PAGE:
            self.layout_box.addItem(f"{n} per page", n)
        self.layout_box.setCurrentIndex(max(0, self.layout_box.findData(options.per_page)))
        self.orientation_box = QComboBox(self)
        self.orientation_box.addItems(["Portrait", "Landscape"])
        self.orientation_box.setCurrentIndex(int(options.landscape))
        self.fit_box = QComboBox(self)
        self.fit_box.addItems(["Fit (show all of the picture)", "Fill (crop to the space)"])
        self.fit_box.setCurrentIndex(int(options.fill))
        self.margin_box = QSpinBox(self)
        self.margin_box.setRange(0, MAX_MARGIN_MM)
        self.margin_box.setSuffix(" mm")
        self.margin_box.setValue(int(options.margin_mm))
        self.captions_box = QCheckBox("Print file names", self)
        self.captions_box.setChecked(options.captions)
        self.rotation_box = QCheckBox("Use the rotation from the viewer", self)
        self.rotation_box.setChecked(options.use_rotation)
        self.rotation_box.setVisible(bool(self.rotations))  # only when a picture is rotated in the viewer
        self.summary = QLabel(self)
        self.summary.setWordWrap(True)
        form = QFormLayout()
        form.addRow("Layout", self.layout_box)
        form.addRow("Orientation", self.orientation_box)
        form.addRow("Picture size", self.fit_box)
        form.addRow("Margin", self.margin_box)
        form.addRow(self.captions_box)
        form.addRow(self.rotation_box)
        right = QVBoxLayout()
        right.addLayout(form)
        right.addWidget(self.summary)
        right.addStretch(1)
        for box in (self.layout_box, self.orientation_box, self.fit_box):
            box.currentIndexChanged.connect(self._refresh)
        self.margin_box.valueChanged.connect(self._refresh)
        for box in (self.captions_box, self.rotation_box):
            box.toggled.connect(self._refresh)

        self.buttons = QDialogButtonBox(self)
        self.print_btn = self.buttons.addButton("Print...", QDialogButtonBox.ButtonRole.AcceptRole)
        self.pdf_btn = self.buttons.addButton("Save as PDF...", QDialogButtonBox.ButtonRole.ActionRole)
        self.buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.print_btn.setDefault(True)
        self.print_btn.clicked.connect(self.print_pages)
        self.pdf_btn.clicked.connect(self.save_pdf)
        self.buttons.rejected.connect(self.reject)

        columns = QHBoxLayout()
        left_box, right_box = QWidget(self), QWidget(self)
        left_box.setLayout(left)
        right_box.setLayout(right)
        left_box.setFixedWidth(240)
        right_box.setFixedWidth(280)
        columns.addWidget(left_box)
        columns.addLayout(middle, 1)
        columns.addWidget(right_box)
        root = QVBoxLayout(self)
        root.addLayout(columns, 1)
        root.addWidget(self.buttons)
        self.resize(1060, 700)
        self._refresh()

    # -- state -------------------------------------------------------------------
    def options(self) -> PrintOptions:
        return PrintOptions(per_page=int(self.layout_box.currentData()),
                            landscape=self.orientation_box.currentIndex() == 1,
                            fill=self.fit_box.currentIndex() == 1, margin_mm=float(self.margin_box.value()),
                            captions=self.captions_box.isChecked(), use_rotation=self.rotation_box.isChecked())

    def selected(self) -> list[Path]:
        return [Path(self.list.item(i).data(PATH_ROLE)) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked]

    def _check_all(self, on: bool) -> None:
        self.list.blockSignals(True)
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        self.list.blockSignals(False)
        self._on_selection_changed()

    def _on_selection_changed(self) -> None:
        self._page = 0
        self._refresh()

    def _step_page(self, delta: int) -> None:
        self._page += delta
        self._refresh()

    def _refresh(self, *_args) -> None:
        paths = self.selected()
        options = self.options()
        per_page = per_page_for(len(paths), options.per_page)
        pages = paginate(paths, per_page)
        self._page = min(max(0, self._page), max(0, len(pages) - 1))
        page = pages[self._page] if pages else []
        self.preview.show_page([self._preview_cell(p, options) for p in page], options, per_page)
        self.page_label.setText(f"Page {self._page + 1} of {len(pages)}" if pages else "No pages")
        self.prev_btn.setEnabled(self._page > 0)
        self.next_btn.setEnabled(self._page < len(pages) - 1)
        has_any = bool(paths)
        self.print_btn.setEnabled(has_any)
        self.pdf_btn.setEnabled(has_any)
        if not has_any:
            self.summary.setText("Select at least 1 picture.")
        else:
            noun = "picture" if len(paths) == 1 else "pictures"
            count = page_count(len(paths), options)
            text = f"{len(paths)} {noun}, {per_page} per page, {count} {'page' if count == 1 else 'pages'} of A4."
            if options.margin_mm < PRINTER_MARGIN_HINT_MM:
                text += "\n\nMost printers cannot print the outer 4 to 5 mm. A PDF uses the margin you set."
            self.summary.setText(text)

    # -- preview loading (background, never blocks the dialog) ---------------------------
    def _preview_cell(self, path: Path, options: PrintOptions) -> Cell:
        key = str(path)
        if key not in self._previews:
            if key not in self._pending:
                self._pending.add(key)
                self._pool.start(_PreviewTask(key, self._signals))
            return Cell(path.name, None, "Loading...")
        self._previews.move_to_end(key)
        img = self._previews[key]
        if img is None:
            return Cell(path.name, None, "No preview")
        rotation = self.rotations.get(path, 0) if options.use_rotation else 0
        return Cell(path.name, img.transformed(QTransform().rotate(rotation)) if rotation else img)

    def _on_preview(self, path: str, img: QImage | None) -> None:
        if self._closed:
            return  # a preview that finished after the dialog closed
        self._pending.discard(path)
        self._previews[path] = img
        while len(self._previews) > PREVIEW_CACHE:
            self._previews.popitem(last=False)
        self._refresh()

    # -- output --------------------------------------------------------------------
    def _run(self, painter: QPainter, device, title: str) -> tuple[bool, list[str]]:
        """Draw all pages with a progress dialog. Returns (finished, failed names)."""
        paths = self.selected()
        progress = QProgressDialog(f"{title}...", "Cancel", 0, len(paths), self)
        progress.setWindowTitle(title)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        # Modal at once: render_document pumps events, and nothing in this dialog may start a second job.
        self._busy, self._progress = True, progress
        self.buttons.setEnabled(False)
        progress.show()

        def report(done: int, total: int) -> bool:
            progress.setValue(done)
            progress.setLabelText(f"{title}: picture {min(done + 1, total)} of {total}")
            return not progress.wasCanceled()

        try:
            _pages, failed = render_document(painter, device, paths, self.options(), self.rotations, report)
        finally:
            self._busy, self._progress = False, None
            self.buttons.setEnabled(True)
        finished = not progress.wasCanceled()
        progress.close()
        progress.deleteLater()
        return finished, failed

    def reject(self) -> None:
        if self._busy and self._progress is not None:
            self._progress.cancel()  # Esc or close during a job stops the job; the dialog stays
            return
        super().reject()

    def _report_failed(self, title: str, failed: list[str]) -> None:
        if failed:
            names = "\n".join(failed[:10]) + (f"\n...and {len(failed) - 10} more" if len(failed) > 10 else "")
            QMessageBox.warning(self, title, f"{APP_NAME} cannot print these files. Their places on the page "
                                             f"are empty.\n\n{names}")

    def _confirm_replace(self, dest: Path) -> bool:
        box = QMessageBox(QMessageBox.Icon.Question, "Save as PDF", f"\"{dest.name}\" already exists. Replace it?",
                          QMessageBox.StandardButton.Cancel, self)
        replace = box.addButton("Replace", QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        return box.clickedButton() is replace

    def print_pages(self) -> None:
        if self._busy:
            return
        options = self.options()
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setDocName(APP_NAME)  # the name in the Windows print queue; never a file name
        prepare_device(printer, options.landscape)
        dialog = QPrintDialog(printer, self)
        dialog.setOption(QAbstractPrintDialog.PrintDialogOption.PrintPageRange, False)
        dialog.setOption(QAbstractPrintDialog.PrintDialogOption.PrintSelection, False)
        dialog.setOption(QAbstractPrintDialog.PrintDialogOption.PrintCurrentPage, False)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        printer.setFullPage(True)  # draw on the whole sheet; render_document keeps clear of the hardware margin
        painter = QPainter()
        if not painter.begin(printer):
            QMessageBox.warning(self, "Print", f"{APP_NAME} cannot start the print job. "
                                               "Make sure that the printer is available. Then try again.")
            return
        finished, failed = self._run(painter, printer, "Print")
        if not finished:
            printer.abort()
        painter.end()
        log.info("Printed %d pictures (finished=%s, failed=%d)", len(self.selected()), finished, len(failed))
        if finished:
            self._report_failed("Print", failed)
            self.accept()

    def save_pdf(self) -> None:
        if self._busy:
            return
        start = str(Path(self.settings.value("last_dir", str(Path.home()))) / "Pictures.pdf")
        target, _ = QFileDialog.getSaveFileName(self, "Save as PDF", start, "PDF document (*.pdf)")
        if not target:
            return
        dest = Path(target)
        if dest.suffix.lower() != ".pdf":
            dest = dest.with_name(dest.name + ".pdf")
            # Windows asked about the name as typed, not about this one.
            if dest.exists() and not self._confirm_replace(dest):
                return
        tmp = dest.with_name(dest.name + ".part")
        writer = QPdfWriter(str(tmp))
        writer.setResolution(PDF_DPI)
        writer.setTitle(dest.stem)
        writer.setCreator(APP_NAME)
        prepare_device(writer, self.options().landscape)
        writer.setPageMargins(QMarginsF(0, 0, 0, 0))
        painter = QPainter()
        if not painter.begin(writer):
            QMessageBox.warning(self, "Save as PDF", f"{APP_NAME} cannot write to this folder. Select a different "
                                                     "folder. Then try again.")
            return
        finished, failed = self._run(painter, writer, "Save as PDF")
        written = painter.end()
        del writer  # close the file before it is moved
        try:
            if finished and not (written and _is_pdf(tmp)):  # for example, the disk is full
                raise OSError(0, "The PDF file is incomplete. Make sure that the disk has free space.")
            if finished:
                tmp.replace(dest)  # atomic: never leaves a half-written PDF under the real name
            else:
                tmp.unlink(missing_ok=True)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            log.warning("Save as PDF failed: %s", exc.strerror)
            QMessageBox.warning(self, "Save as PDF", f"{APP_NAME} did not save the PDF. {exc.strerror or exc}")
            return
        if finished:
            log.info("Saved PDF %s", describe_file(dest))
            self._report_failed("Save as PDF", failed)
            self.accept()

    def done(self, result: int) -> None:
        write_options(self.settings, self.options())  # Cancel also keeps the layout the user chose
        self._pool.clear()  # queued previews never start; a running one finishes into the void
        self._closed = True
        self._previews.clear()
        self._pending.clear()
        super().done(result)


def _is_pdf(path: Path) -> bool:
    """A finished PDF starts with %PDF and ends with %%EOF (a full disk cuts the end off)."""
    try:
        with path.open("rb") as f:
            head = f.read(5)
            f.seek(max(0, path.stat().st_size - 32))
            return head == b"%PDF-" and b"%%EOF" in f.read()
    except OSError:
        return False
