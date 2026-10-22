"""Tests for 1.2: preload cache, welcome screen, open folder, full screen, badges, slideshow."""

import os
import shutil
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QElapsedTimer, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QApplication, QFileDialog, QToolBar

from mediareaderx.cache import DecodedCache
from mediareaderx.decoders import decode
from mediareaderx.folder import list_media
from mediareaderx.main_window import MainWindow
from mediareaderx.widgets.filmstrip import compose_tile, format_label


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
def window(qapp):
    w = MainWindow()
    w.show()
    yield w
    w.close()


def _image_shown(w):
    return w.stack.currentIndex() == w.PAGE_IMAGE and w._current_image is not None


# -- 1. preload cache ----------------------------------------------------------------
def test_cache_get_put_discard_and_budget(samples, qapp):
    img = decode(samples / "sample.png")
    cost = sum(f.sizeInBytes() for f in img.frames)
    cache = DecodedCache(budget_bytes=cost * 2)
    a, b, c = samples / "sample.png", samples / "sample.avif", samples / "sample.heic"
    for p in (a, b, c):
        cache.put(p, img)
    assert a not in cache and b in cache and c in cache  # oldest evicted
    assert cache.used_bytes <= cost * 2
    cache.discard(b)
    assert b not in cache


def test_cache_misses_after_the_file_changes(tmp_path, samples, qapp):
    path = tmp_path / "x.png"
    shutil.copy(samples / "sample.png", path)
    cache = DecodedCache()
    cache.put(path, decode(path))
    assert cache.get(path) is not None
    time.sleep(0.02)
    path.write_bytes(path.read_bytes())  # new modification time
    os.utime(path, None)
    assert cache.get(path) is None


def test_next_image_is_preloaded_and_shown_at_once(window, folder):
    images = [p for p in list_media(folder) if p.suffix.lower() in (".avif", ".gif", ".heic", ".jxl", ".png")]
    window.open_path(images[0])
    assert wait_until(lambda: _image_shown(window))
    nxt = window.nav.files[window.nav.index + 1]
    if nxt.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm"):
        pytest.skip("neighbour is a video in this sample order")
    assert wait_until(lambda: nxt in window._cache)
    window.act_next.trigger()
    assert _image_shown(window) and window.current == nxt  # no event loop needed: served from cache


# -- 2. welcome screen ----------------------------------------------------------------
def test_welcome_screen_on_start_with_recent_files(window, folder):
    assert window.stack.currentIndex() == window.PAGE_WELCOME
    assert not window.welcome.recent_title.isVisible()  # nothing recent yet
    window.open_path(folder / "sample.png")
    window._show_welcome()
    assert window.welcome.recent_buttons and window.welcome.recent_title.isVisible()
    window.welcome.recent_buttons[0].click()
    assert window.current == folder / "sample.png"


def test_welcome_primary_button_is_open_file(window):
    assert window.welcome.open_file_btn.objectName() == "primary"
    assert window.welcome.open_file_btn.isDefault()


# -- 3. open folder --------------------------------------------------------------------
def test_open_folder_shows_first_file_in_explorer_order(window, folder):
    window.open_path(folder)  # a folder path, as from the command line or drag and drop
    assert window.current == list_media(folder)[0]


def test_open_folder_dialog(window, folder, monkeypatch):
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(folder))
    window.act_open_folder.trigger()
    assert window.current == list_media(folder)[0]


def test_empty_folder_message(window, tmp_path):
    empty = tmp_path / "Empty"
    empty.mkdir()
    window.open_folder(empty)
    assert window.stack.currentIndex() == window.PAGE_MESSAGE
    assert "has no photos or videos" in window.message.text()


# -- 7. distraction-free full screen -------------------------------------------------------
def test_full_screen_hides_chrome_and_restores_it(window, folder):
    window.open_path(folder / "sample.png")
    toolbar = window.findChild(QToolBar, "MainToolbar")
    window.act_full.trigger()
    assert window.immersive
    assert not window.menuBar().isVisible() and not toolbar.isVisible()
    assert not window.meta_panel.isVisible() and not window.filmstrip.isVisible()
    assert window.overlay.isVisible() and "sample.png" in window.overlay.text()
    window._on_escape()
    assert not window.immersive
    assert window.menuBar().isVisible() and toolbar.isVisible() and window.meta_panel.isVisible()


def test_full_screen_idle_hides_cursor_and_video_controls(window, folder):
    window.open_path(folder / "hevc.mp4")
    window.toggle_fullscreen(True)
    window._on_idle()
    assert QApplication.overrideCursor() is not None and not window.video_view.controls.isVisible()
    window._wake()
    assert QApplication.overrideCursor() is None and window.video_view.controls.isVisible()
    window.toggle_fullscreen(False)


# -- 8. thumbnail badges ------------------------------------------------------------------
def test_format_labels():
    assert format_label(Path("a.HEIC")) == "HEIC"
    assert format_label(Path("a.jpeg")) == "JPG"
    assert format_label(Path("a.NEF")) == "RAW"
    assert format_label(Path("a.mp4")) == "MP4"
    assert format_label(Path("a.mov"), 75_000) == "MOV  1:15"


def test_compose_tile_draws_badge(qapp):
    icon = compose_tile(QImage(), "HEIC", video=False)
    assert not icon.isNull()
    img = QImage(64, 48, QImage.Format.Format_RGB32)
    img.fill(Qt.GlobalColor.white)
    assert not compose_tile(img, "MOV  0:04", video=True).isNull()


def test_strip_items_have_accessible_text(window, folder):
    window.open_path(folder / "sample.heic")
    texts = [window.filmstrip.list.item(i).data(Qt.ItemDataRole.AccessibleTextRole)
             for i in range(window.filmstrip.list.count())]
    assert "sample.heic, HEIC" in texts


# -- 9. slideshow ----------------------------------------------------------------------
@pytest.fixture
def fast_slides(monkeypatch):
    monkeypatch.setattr(MainWindow, "slideshow_seconds", property(lambda self: 0))


def test_slideshow_advances_and_wraps(window, folder, fast_slides):
    files = list_media(folder)
    window.open_path(files[-1])
    window.act_slideshow.trigger()
    assert window.immersive
    assert wait_until(lambda: window.current == files[0])  # wrapped from the last file to the first
    window._on_escape()
    assert not window.act_slideshow.isChecked() and not window.immersive


def test_slideshow_plays_videos_to_the_end_without_loop(window, folder, fast_slides):
    window.video_view.loop_btn.setChecked(True)
    window.open_path(folder / "hevc.mp4")
    window.act_slideshow.trigger()
    assert window.video_view.player.loops() == QMediaPlayer.Loops.Once
    assert wait_until(lambda: window.current != folder / "hevc.mp4", 10000)  # moved on after the clip ended
    window.toggle_slideshow(False)
    assert window.video_view.suppress_loop is False


def test_slideshow_needs_a_file(window):
    window.act_slideshow.trigger()
    assert not window.act_slideshow.isChecked() and not window.immersive


# -- polish found in the packaged-app check ----------------------------------------------
def test_welcome_hides_empty_panels_and_restores_them(window, folder):
    assert window.stack.currentIndex() == window.PAGE_WELCOME
    assert window.meta_panel.isHidden() and window.filmstrip.isHidden()
    window.open_path(folder / "sample.png")
    assert not window.meta_panel.isHidden() and not window.filmstrip.isHidden()


def test_full_screen_uses_black_backdrop(window, folder):
    window.open_path(folder / "sample.png")
    window.toggle_fullscreen(True)
    assert window.image_view.backgroundBrush().color() == QColor("black")
    window.toggle_fullscreen(False)
    assert window.image_view.backgroundBrush().style() == Qt.BrushStyle.NoBrush
