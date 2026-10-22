"""Tests for 1.3: print several pictures on one A4 page, preview, PDF output, strip multi-select."""

import re
import shutil
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QElapsedTimer, QPointF, QRectF, QSettings, QSizeF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPdfWriter
from PySide6.QtTest import QTest

from mediareaderx.main_window import MainWindow
from mediareaderx.printing import (Cell, PrintOptions, cell_rects, crop_rect, fit_rect, grid_for, load_for_print,
                                   page_count, paginate, per_page_for, picture_box, prepare_device, render_document,
                                   render_page)
from mediareaderx.widgets.print_dialog import PrintDialog, read_options, write_options


def wait_until(predicate, timeout_ms=8000):
    timer = QElapsedTimer()
    timer.start()
    while not predicate():
        if timer.elapsed() > timeout_ms:
            return False
        QCoreApplication.processEvents()
    return True


@pytest.fixture
def folder(samples, tmp_path):
    dest = tmp_path / "album"
    shutil.copytree(samples, dest)
    return dest


@pytest.fixture
def settings(tmp_path):
    return QSettings(str(tmp_path / "print.ini"), QSettings.Format.IniFormat)


def _pictures(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in (".png", ".jpg", ".heic", ".avif", ".webp"))


# -- layout maths -----------------------------------------------------------------------
@pytest.mark.parametrize("count, expected", [(0, 1), (1, 1), (2, 2), (3, 4), (5, 6), (7, 9), (9, 9), (25, 9)])
def test_auto_picks_the_smallest_grid(count, expected):
    assert per_page_for(count, 0) == expected


def test_fixed_choice_wins_and_bad_choice_means_auto():
    assert per_page_for(1, 6) == 6
    assert per_page_for(3, 7) == 4  # not a grid: Auto


def test_grids_turn_with_the_page():
    assert grid_for(2, landscape=False) == (1, 2)
    assert grid_for(2, landscape=True) == (2, 1)
    assert grid_for(6, landscape=False) == (2, 3)
    assert grid_for(6, landscape=True) == (3, 2)
    assert grid_for(9, landscape=True) == (3, 3)


def test_pagination_and_page_count():
    assert paginate(list(range(10)), 4) == [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9]]
    assert paginate([], 4) == []
    assert page_count(10, PrintOptions(per_page=4)) == 3
    assert page_count(10, PrintOptions(per_page=0)) == 2  # Auto: 9 per page
    assert page_count(0, PrintOptions()) == 0


def test_cells_fill_the_area_with_gaps():
    rects = cell_rects(QRectF(0, 0, 100, 210), 2, 3, gap=5)
    assert len(rects) == 6
    assert rects[0] == QRectF(0, 0, 47.5, 66.666666666666671)
    assert rects[1].left() == pytest.approx(52.5)
    assert rects[-1].right() == pytest.approx(100)
    assert rects[-1].bottom() == pytest.approx(210)


def test_fit_keeps_the_whole_picture_and_fill_crops_the_centre():
    box = QRectF(0, 0, 100, 100)
    fit = fit_rect(QSizeF(400, 200), box)
    assert fit == QRectF(0, 25, 100, 50)
    crop = crop_rect(QSizeF(400, 200), box)
    assert crop == QRectF(100, 0, 200, 200)  # the square middle of a 2:1 picture
    assert fit_rect(QSizeF(0, 0), box).isEmpty()


def test_caption_takes_a_strip_from_the_cell():
    cell = QRectF(0, 0, 100, 100)
    assert picture_box(cell, 1.0, captions=False) == cell
    assert picture_box(cell, 1.0, captions=True).height() == pytest.approx(94)
    assert picture_box(QRectF(0, 0, 10, 4), 1.0, captions=True).height() == pytest.approx(2)  # never all of it


# -- drawing -----------------------------------------------------------------------------
def _solid(color: str, w=40, h=20) -> QImage:
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(color))
    return img


def _render(cells, options, per_page, size=(210, 297)) -> QImage:
    page = QImage(*size, QImage.Format.Format_RGB32)
    page.fill(QColor("white"))
    p = QPainter(page)
    render_page(p, QRectF(0, 0, *size), 1.0, cells, options, per_page)
    p.end()
    return page


def test_render_places_pictures_in_grid_order(qapp):
    cells = [Cell("a", _solid("red")), Cell("b", _solid("blue")), Cell("c", _solid("lime")), Cell("d", _solid("black"))]
    page = _render(cells, PrintOptions(margin_mm=10, fill=True), 4)
    # 2 x 2 inside 10 mm margins: centre of each quarter
    assert page.pixelColor(57, 80) == QColor("red")
    assert page.pixelColor(152, 80) == QColor("blue")
    assert page.pixelColor(57, 220) == QColor("lime")
    assert page.pixelColor(152, 220) == QColor("black")
    assert page.pixelColor(3, 3) == QColor("white")  # the margin stays empty


def test_fit_leaves_paper_and_fill_covers_the_cell(qapp):
    wide = [Cell("wide", _solid("red", 400, 100))]
    fit = _render(wide, PrintOptions(margin_mm=0, fill=False), 1)
    fill = _render(wide, PrintOptions(margin_mm=0, fill=True), 1)
    assert fit.pixelColor(105, 148) == QColor("red")
    assert fit.pixelColor(105, 20) == QColor("white")  # above the letterboxed picture
    assert fill.pixelColor(105, 20) == QColor("red")


def test_missing_picture_draws_a_placeholder_with_a_note(qapp):
    page = _render([Cell("x.heic", None, "Media Reader x cannot print this file.")], PrintOptions(margin_mm=0), 1)
    assert page.pixelColor(10, 10) != QColor("white")


def test_captions_draw_the_file_name(qapp):
    def dark_pixels_in_caption(captions: bool) -> int:
        page = QImage(840, 1188, QImage.Format.Format_RGB32)  # 4 px per mm, so the 3 mm text is 12 px
        page.fill(QColor("white"))
        p = QPainter(page)
        render_page(p, QRectF(0, 0, 840, 1188), 4.0, [Cell("IMG_0001.heic", _solid("white"))],
                    PrintOptions(margin_mm=0, captions=captions), 1)
        p.end()
        # The picture is white, so every dark pixel is caption text.
        return sum(page.pixelColor(x, y).lightness() < 160 for x in range(0, 840, 2) for y in range(0, 1188, 2))

    assert dark_pixels_in_caption(True) > 10
    assert dark_pixels_in_caption(False) == 0


def test_caption_sits_directly_under_a_fitted_picture(qapp):
    page = QImage(840, 1188, QImage.Format.Format_RGB32)
    page.fill(QColor("white"))
    p = QPainter(page)
    render_page(p, QRectF(0, 0, 840, 1188), 4.0, [Cell("IMG_0001.heic", _solid("red", 400, 100))],
                PrintOptions(margin_mm=0, captions=True), 1)
    p.end()
    red_bottom = max(y for y in range(1188) if page.pixelColor(420, y) == QColor("red"))
    dark = [y for y in range(1188) for x in range(300, 540, 2) if page.pixelColor(x, y).lightness() < 160
            and page.pixelColor(x, y) != QColor("red")]
    assert dark and red_bottom < min(dark) < red_bottom + 6 * 4  # within the 6 mm caption strip


# -- output ------------------------------------------------------------------------------
def test_load_for_print_shrinks_rotates_and_never_raises(samples, qapp):
    cell = load_for_print(samples / "sample.png", 90, QSizeF(10, 10), fill=False)
    assert cell.image is not None and max(cell.image.width(), cell.image.height()) <= 10
    broken = samples.parent / "broken.heic"
    broken.write_bytes(b"not an image")
    bad = load_for_print(broken, 0, QSizeF(100, 100), fill=False)
    assert bad.image is None and "cannot print" in bad.note


def test_rotation_is_applied_to_the_printed_picture(samples, qapp):
    src = load_for_print(samples / "sample.png", 0, QSizeF(5000, 5000), fill=False).image
    turned = load_for_print(samples / "sample.png", 90, QSizeF(5000, 5000), fill=False).image
    assert (turned.width(), turned.height()) == (src.height(), src.width())


def _write_pdf(path: Path, paths, options, rotations=None, progress=None):
    writer = QPdfWriter(str(path))
    writer.setResolution(72)
    prepare_device(writer, options.landscape)
    p = QPainter(writer)
    result = render_document(p, writer, paths, options, rotations or {}, progress)
    p.end()
    del writer
    return result


def test_pdf_has_one_page_per_grid_and_reports_failures(folder, qapp, tmp_path):
    pictures = _pictures(folder)[:3]
    broken = folder / "broken.heic"
    broken.write_bytes(b"not an image")
    pages, failed = _write_pdf(tmp_path / "out.pdf", [*pictures, broken], PrintOptions(per_page=2))
    data = (tmp_path / "out.pdf").read_bytes()
    assert data.startswith(b"%PDF")
    assert pages == 2
    assert len(re.findall(rb"/Type\s*/Page[^s]", data)) == 2
    assert failed == ["broken.heic"]


def test_cancel_stops_the_document(folder, qapp, tmp_path):
    pictures = _pictures(folder)
    pages, _failed = _write_pdf(tmp_path / "c.pdf", pictures, PrintOptions(per_page=1), progress=lambda d, t: d < 1)
    assert pages < len(pictures)


# -- settings ------------------------------------------------------------------------
def test_options_round_trip_and_damaged_values_fall_back(settings):
    write_options(settings, PrintOptions(per_page=6, landscape=True, fill=True, margin_mm=5, captions=True,
                                         use_rotation=False))
    assert read_options(settings) == PrintOptions(6, True, True, 5, True, False)
    settings.setValue("print/per_page", "seven")
    settings.setValue("print/margin_mm", 500)
    settings.setValue("print/landscape", "maybe")
    opts = read_options(settings)
    assert opts.per_page == 0 and opts.margin_mm == 10 and opts.landscape is False
    settings.setValue("print/per_page", 7)  # a number, but not a grid
    assert read_options(settings).per_page == 0


# -- dialog ---------------------------------------------------------------------------
def test_dialog_lists_pictures_ticks_the_chosen_and_previews(folder, settings, qapp):
    files = _pictures(folder)
    dlg = PrintDialog(files, {files[0], files[2]}, {}, settings)
    dlg.show()
    assert dlg.list.count() == len(files)
    assert dlg.selected() == [files[0], files[2]]
    assert "2 pictures, 2 per page, 1 page" in dlg.summary.text()
    assert not dlg.rotation_box.isVisibleTo(dlg)  # nothing rotated in the viewer
    assert wait_until(lambda: all(c.image is not None for c in dlg.preview.cells))
    dlg._check_all(True)
    assert len(dlg.selected()) == len(files)
    dlg._check_all(False)
    assert not dlg.print_btn.isEnabled() and not dlg.pdf_btn.isEnabled()
    assert "Select at least 1 picture" in dlg.summary.text()
    dlg.reject()


def test_dialog_pages_through_the_preview(folder, settings, qapp):
    files = _pictures(folder)
    dlg = PrintDialog(files, set(files), {}, settings)
    dlg.layout_box.setCurrentIndex(dlg.layout_box.findData(1))
    assert dlg.page_label.text() == f"Page 1 of {len(files)}"
    assert not dlg.prev_btn.isEnabled()
    dlg.next_btn.click()
    assert dlg.page_label.text() == f"Page 2 of {len(files)}"
    assert dlg.preview.cells[0].name == files[1].name
    dlg.reject()
    assert read_options(settings).per_page == 1  # the layout choice is kept


def test_dialog_save_pdf_writes_the_file_atomically(folder, settings, qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    files = _pictures(folder)
    target = tmp_path / "out" / "album"
    target.parent.mkdir()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    dlg = PrintDialog(files, set(files), {}, settings)
    dlg.save_pdf()
    pdf = target.with_name("album.pdf")
    assert pdf.read_bytes().startswith(b"%PDF")
    assert not list(target.parent.glob("*.part"))


def test_preview_uses_the_viewer_rotation(folder, settings, qapp):
    files = _pictures(folder)
    dlg = PrintDialog(files, {files[0]}, {files[0]: 90}, settings)
    assert dlg.rotation_box.isVisibleTo(dlg)
    assert wait_until(lambda: dlg.preview.cells and dlg.preview.cells[0].image is not None)
    raw = dlg._previews[str(files[0])]
    shown = dlg.preview.cells[0].image
    assert (shown.width(), shown.height()) == (raw.height(), raw.width())
    dlg.rotation_box.setChecked(False)
    assert dlg.preview.cells[0].image.size() == raw.size()
    dlg.reject()


# -- main window and strip ------------------------------------------------------------
@pytest.fixture
def window(qapp):
    w = MainWindow()
    w.show()
    yield w
    w.close()


def _click(strip, row, modifier=Qt.KeyboardModifier.NoModifier):
    rect = strip.list.visualItemRect(strip.list.item(row))
    QTest.mouseClick(strip.list.viewport(), Qt.MouseButton.LeftButton, modifier, rect.center())


def test_ctrl_click_selects_without_opening(folder, window, qapp):
    window.open_path(_pictures(folder)[0])
    assert wait_until(lambda: window._current_image is not None)
    files = window.nav.files
    current = window.current
    other = next(i for i, p in enumerate(files) if p != current)
    _click(window.filmstrip, other, Qt.KeyboardModifier.ControlModifier)
    QCoreApplication.processEvents()
    assert window.current == current  # not opened
    assert set(window.filmstrip.selected_files()) == {current, files[other]}
    _click(window.filmstrip, other)  # a plain click opens it and resets the selection
    assert wait_until(lambda: window.current == files[other])
    assert window.filmstrip.selected_files() == [files[other]]


def test_print_action_offers_only_pictures(folder, window, qapp, monkeypatch):
    seen = {}

    def fake_exec(dlg):
        seen["files"] = [Path(dlg.list.item(i).data(Qt.ItemDataRole.UserRole)) for i in range(dlg.list.count())]
        seen["checked"] = dlg.selected()
        return 0

    monkeypatch.setattr(PrintDialog, "exec", fake_exec)
    assert not window.act_print.isEnabled()  # welcome screen: nothing to print
    window.open_path(_pictures(folder)[0])
    assert window.act_print.isEnabled()
    window.act_print.trigger()
    assert seen["checked"] == [window.current]
    assert all(p.suffix.lower() not in (".mp4", ".mov", ".mkv", ".webm") for p in seen["files"])
    assert len(seen["files"]) < len(window.nav.files)  # the sample videos are left out


# -- review fixes ---------------------------------------------------------------------
def test_print_dialog_is_deleted_after_use(folder, window, qapp, monkeypatch):
    from PySide6.QtCore import QEvent

    monkeypatch.setattr(PrintDialog, "exec", lambda dlg: 0)
    window.open_path(_pictures(folder)[0])
    for _ in range(3):
        window.act_print.trigger()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert window.findChildren(PrintDialog) == []


def test_no_second_job_and_esc_cancels_while_busy(folder, settings, qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from mediareaderx.widgets import print_dialog

    files = _pictures(folder)
    dlg = PrintDialog(files, set(files), {}, settings)
    seen = {}

    def fake_render(painter, device, paths, options, rotations, progress):
        seen["buttons"] = dlg.buttons.isEnabled()
        seen["modal"] = dlg._progress.isVisible()
        dlg.save_pdf()  # a second click does nothing
        dlg.reject()  # Esc cancels the job, not the dialog
        return 0, []

    target = tmp_path / "busy.pdf"
    calls = []
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (calls.append(1), (str(target), ""))[1])
    monkeypatch.setattr(print_dialog, "render_document", fake_render)
    dlg.save_pdf()
    assert seen == {"buttons": False, "modal": True}
    assert calls == [1]
    assert not target.exists() and not list(tmp_path.glob("*.part"))  # cancelled: nothing saved
    assert dlg.result() == 0 and not dlg._closed  # the dialog is still open
    assert dlg.buttons.isEnabled()
    dlg.reject()
    assert dlg._closed


def test_cancel_returns_without_waiting_for_running_decodes(folder, qapp, tmp_path, monkeypatch):
    import time

    from mediareaderx import printing

    def slow(path, rotation, max_size, fill):
        time.sleep(1.5)
        return Cell(path.name, None, "")

    monkeypatch.setattr(printing, "load_for_print", slow)
    start = time.perf_counter()
    _write_pdf(tmp_path / "x.pdf", _pictures(folder), PrintOptions(per_page=4), progress=lambda d, t: False)
    assert time.perf_counter() - start < 1.0


def test_pdf_without_extension_asks_before_replacing(folder, settings, qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    existing = tmp_path / "album.pdf"
    existing.write_bytes(b"keep me")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(tmp_path / "album"), ""))
    files = _pictures(folder)
    dlg = PrintDialog(files, set(files), {}, settings)
    monkeypatch.setattr(dlg, "_confirm_replace", lambda dest: False)
    dlg.save_pdf()
    assert existing.read_bytes() == b"keep me"
    monkeypatch.setattr(dlg, "_confirm_replace", lambda dest: True)
    dlg.save_pdf()
    assert existing.read_bytes().startswith(b"%PDF")


def test_truncated_pdf_is_not_kept(tmp_path):
    from mediareaderx.widgets.print_dialog import _is_pdf

    good = tmp_path / "g.pdf"
    good.write_bytes(b"%PDF-1.4\n...\n%%EOF\n")
    cut = tmp_path / "c.pdf"
    cut.write_bytes(b"%PDF-1.4\n... (disk full)")
    assert _is_pdf(good) and not _is_pdf(cut) and not _is_pdf(tmp_path / "missing.pdf")


def test_releasing_ctrl_before_the_button_does_not_open(folder, window, qapp):
    window.open_path(_pictures(folder)[0])
    assert wait_until(lambda: window._current_image is not None)
    files, current = window.nav.files, window.current
    other = next(i for i, p in enumerate(files) if p != current)
    strip = window.filmstrip
    point = strip.list.visualItemRect(strip.list.item(other)).center()
    QTest.mousePress(strip.list.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, point)
    QTest.mouseRelease(strip.list.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    QCoreApplication.processEvents()
    assert window.current == current


def test_grid_follows_the_sheet_the_printer_uses(folder, qapp, tmp_path, monkeypatch):
    from mediareaderx import printing

    seen = []
    real = printing.render_page
    monkeypatch.setattr(printing, "render_page",
                        lambda p, paper, mm, cells, options, per_page, **k: (seen.append(options.landscape),
                                                                             real(p, paper, mm, cells, options,
                                                                                  per_page, **k)))
    writer = QPdfWriter(str(tmp_path / "l.pdf"))
    prepare_device(writer, landscape=True)  # the Windows dialog turned the sheet
    p = QPainter(writer)
    render_document(p, writer, _pictures(folder)[:2], PrintOptions(per_page=2, landscape=False))
    p.end()
    assert seen == [True]


def test_failed_file_leaves_paper_empty_but_preview_shows_a_box(qapp):
    cells = [Cell("x.heic", None, "")]
    paper = QImage(210, 297, QImage.Format.Format_RGB32)
    paper.fill(QColor("white"))
    p = QPainter(paper)
    render_page(p, QRectF(0, 0, 210, 297), 1.0, cells, PrintOptions(margin_mm=0), 1, placeholders=False)
    p.end()
    assert paper.pixelColor(100, 100) == QColor("white")
    assert _render(cells, PrintOptions(margin_mm=0), 1).pixelColor(100, 100) != QColor("white")


def test_rotated_picture_is_scaled_to_the_cell(samples, qapp):
    cell = load_for_print(samples / "sample.png", 90, QSizeF(10, 30), fill=False)
    assert cell.image.width() <= 10 and cell.image.height() <= 30
    assert cell.image.height() > cell.image.width()  # a landscape sample, turned upright
