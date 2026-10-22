"""End-to-end checks of the main window running on Qt's offscreen platform."""

import pytest
from PySide6.QtCore import QCoreApplication, QElapsedTimer
from PySide6.QtMultimedia import QMediaPlayer

from mediareaderx.main_window import MainWindow


def wait_until(predicate, timeout_ms=8000):
    timer = QElapsedTimer()
    timer.start()
    while not predicate():
        if timer.elapsed() > timeout_ms:
            return False
        QCoreApplication.processEvents()
    return True


@pytest.fixture
def window(qapp):
    w = MainWindow()
    w.show()
    yield w
    w.close()


def test_opens_heic_and_fills_metadata(window, samples):
    window.open_path(samples / "sample.heic")
    assert wait_until(lambda: window.stack.currentIndex() == window.PAGE_IMAGE)
    assert window.meta_panel.tree.topLevelItemCount() >= 3
    assert "320 x 240" in window.status.text()


@pytest.mark.parametrize("name", ["hevc.mp4", "hevc.mov", "vp9.webm", "av1.mkv", "h264.mp4"])
def test_plays_modern_video(window, samples, name):
    window.open_path(samples / name)
    player = window.video_view.player
    assert wait_until(lambda: player.mediaStatus() in (
        QMediaPlayer.MediaStatus.BufferedMedia, QMediaPlayer.MediaStatus.EndOfMedia))
    assert player.error() == QMediaPlayer.Error.NoError
    assert player.hasVideo()
    assert wait_until(lambda: window.meta_panel.tree.topLevelItemCount() >= 2)
    rows = window.meta_panel._rows(only_selected=False)
    assert "Video codec" in rows and "320 x 240" in rows


def test_live_photo_toggle(window, samples):
    window.open_path(samples / "IMG_0001.HEIC")
    assert wait_until(lambda: window.stack.currentIndex() == window.PAGE_IMAGE)
    assert window.act_live.isEnabled()
    window.act_live.trigger()
    assert window.stack.currentIndex() == window.PAGE_VIDEO
    window.act_live.trigger()
    assert window.stack.currentIndex() == window.PAGE_IMAGE


def test_unsupported_and_missing_files_show_message(window, tmp_path):
    window.open_path(tmp_path / "doc.txt")
    assert window.stack.currentIndex() == window.PAGE_MESSAGE
    window.open_path(tmp_path / "gone.heic")
    assert window.stack.currentIndex() == window.PAGE_MESSAGE
    assert "cannot be found" in window.message.text()


def test_stale_decode_results_are_ignored(window, samples):
    window.open_path(samples / "sample.avif")
    window.open_path(samples / "sample.jxl")
    assert wait_until(lambda: window.stack.currentIndex() == window.PAGE_IMAGE)
    wait_until(lambda: False, 500)  # let the first task finish too
    assert "sample.jxl" in window.status.text()
