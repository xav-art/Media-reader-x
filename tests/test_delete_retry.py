"""Move to Recycle Bin waits while a file is briefly open, and never leaves a Live Photo half deleted.

Found on 2026-11-05: on a busy PC (antivirus scanning fresh test files) the delete test sometimes got a failed move,
and the app's warning dialog then hung the whole run. The app now waits up to 2 s while it processes its own events
(our RAW/PSD decodes and video thumbnail grabs hold files that long); conftest turns any message box into a test
failure instead of a hang.
"""

import os
import shutil

import pytest
from PySide6.QtCore import QCoreApplication, QElapsedTimer, QFile, QTimer
from PySide6.QtWidgets import QMessageBox

from mediareaderx import main_window
from mediareaderx.main_window import MainWindow, _move_to_trash


def _remove(path):
    """Stands in for the Recycle Bin (tests must not fill the real one), and fails like it while a file is open."""
    try:
        os.remove(path)
        return True
    except PermissionError:
        return False


@pytest.fixture
def jpg(tmp_path):
    f = tmp_path / "photo.jpg"
    f.write_bytes(b"x")
    return f


def test_a_brief_lock_is_waited_out(jpg, monkeypatch):
    calls = []
    monkeypatch.setattr(QFile, "moveToTrash", staticmethod(lambda path: calls.append(path) or len(calls) >= 3))
    assert _move_to_trash(jpg)
    assert len(calls) == 3


def test_our_own_events_run_while_waiting(qapp, jpg, monkeypatch):
    # A video thumbnail grab runs on the GUI thread: only our event loop can make it close the file.
    closed = []
    QTimer.singleShot(150, lambda: closed.append(True))
    monkeypatch.setattr(QFile, "moveToTrash", staticmethod(lambda path: bool(closed)))
    assert _move_to_trash(jpg)


def test_a_lasting_lock_gives_up_at_the_deadline(jpg, monkeypatch):
    monkeypatch.setattr(main_window, "TRASH_WAIT_MS", 300)
    monkeypatch.setattr(QFile, "moveToTrash", staticmethod(lambda path: False))
    t = QElapsedTimer()
    t.start()
    assert not _move_to_trash(jpg)
    assert 300 <= t.elapsed() < 3000
    assert jpg.exists()  # never deleted any other way


def test_a_file_that_vanished_is_not_reported_as_moved(tmp_path, monkeypatch):
    monkeypatch.setattr(QFile, "moveToTrash", staticmethod(lambda path: False))
    assert not _move_to_trash(tmp_path / "never-there.jpg")


def test_message_boxes_never_block_a_test_run(qapp, _no_blocking_message_boxes):
    assert QMessageBox.warning(None, "Title", "Text") == QMessageBox.StandardButton.Ok  # returns at once
    assert _no_blocking_message_boxes == [("warning", "Title", "Text")]
    _no_blocking_message_boxes.clear()  # expected here: the fixture fails the test only for unread boxes


# -- the window ------------------------------------------------------------------------
@pytest.fixture
def live(samples, tmp_path):
    album = tmp_path / "album"
    album.mkdir()
    for name in ("IMG_0001.HEIC", "IMG_0001.MOV", "sample.png"):
        shutil.copy(samples / name, album / name)
    return album


@pytest.fixture
def window(qapp):
    w = MainWindow()
    w.show()
    yield w
    w.close()


def _open_photo(window, path):
    window.open_path(path)
    t = QElapsedTimer()
    t.start()
    while window.stack.currentIndex() != window.PAGE_IMAGE and t.elapsed() < 15000:
        QCoreApplication.processEvents()
    assert window.companion is not None


def test_live_photo_video_stays_when_the_photo_cannot_move(window, live, monkeypatch, _no_blocking_message_boxes):
    monkeypatch.setattr(main_window, "TRASH_WAIT_MS", 100)
    moved = []
    monkeypatch.setattr(QFile, "moveToTrash", staticmethod(lambda path: moved.append(path) or False))
    _open_photo(window, live / "IMG_0001.HEIC")
    window.act_delete.trigger()
    assert [box[1] for box in _no_blocking_message_boxes] == ["Move to Recycle Bin"]
    _no_blocking_message_boxes.clear()
    assert all(p.endswith(".HEIC") for p in moved)  # the clip was never tried on its own
    assert (live / "IMG_0001.HEIC").exists() and (live / "IMG_0001.MOV").exists()


def test_a_live_photo_video_that_cannot_move_is_reported(window, live, monkeypatch):
    monkeypatch.setattr(main_window, "TRASH_WAIT_MS", 100)

    def photo_only(path):
        if path.endswith(".MOV"):
            return False
        return _remove(path)

    monkeypatch.setattr(QFile, "moveToTrash", staticmethod(photo_only))
    _open_photo(window, live / "IMG_0001.HEIC")
    window.act_delete.trigger()
    msg = window.statusBar().currentMessage()
    assert "Moved IMG_0001.HEIC to the Recycle Bin." in msg and "IMG_0001.MOV is still in the folder" in msg
    assert not (live / "IMG_0001.HEIC").exists() and (live / "IMG_0001.MOV").exists()
    assert window.current == live / "sample.png"


def test_a_file_gone_without_confirmation_is_said_honestly(window, live, monkeypatch):
    def vanish(path):
        try:
            os.remove(path)  # Windows removed it but reported a failure (for example a volume with no Recycle Bin)
        except PermissionError:
            pass  # still open (our own decode had not finished): a locked file, so the app tries again
        return False

    monkeypatch.setattr(QFile, "moveToTrash", staticmethod(vanish))
    window.open_path(live / "sample.png")
    window.act_delete.trigger()
    assert "did not confirm that it is in the Recycle Bin" in window.statusBar().currentMessage()
    assert window.current != live / "sample.png"


class _Drop:
    def __init__(self):
        self.ignored = False

    def ignore(self):
        self.ignored = True

    def mimeData(self):
        raise AssertionError("a drop during a delete must not be read")


def test_no_drop_opens_a_file_during_the_wait(window, monkeypatch):
    opened = []
    monkeypatch.setattr(window, "open_path", opened.append)
    window._deleting = True
    drop = _Drop()
    window.dropEvent(drop)
    window._deleting = False
    assert drop.ignored and opened == []


@pytest.mark.parametrize("unlocks", [True, False])
def test_a_close_during_the_wait_happens_after_it(window, live, monkeypatch, unlocks):
    monkeypatch.setattr(main_window, "TRASH_WAIT_MS", 300)
    target = live / "sample.png"
    calls = []

    def locked_then(path):
        calls.append(path)
        if len(calls) == 1:
            assert not window.close()  # the taskbar close arrives during the wait: refused for now
            assert window.isVisible()
            return False
        if unlocks:
            return _remove(path)
        return False  # still locked: the failure box must not show on a closing window

    monkeypatch.setattr(QFile, "moveToTrash", staticmethod(locked_then))
    window.open_path(target)
    window.act_delete.trigger()
    assert not window.isVisible()  # closed as soon as the delete finished
    assert target.exists() is not unlocks
    assert window.current == target  # no other file was opened on the closing window


def test_the_strip_does_not_reopen_a_file_being_deleted(window, live):
    window.open_path(live / "sample.png")
    strip = window.filmstrip
    clip = live / "IMG_0001.MOV"  # hidden as a Live Photo clip: use a visible file instead
    target = live / "sample.png"
    strip.invalidate(target)
    strip._tasks.pop(str(target), None)
    strip.release(target)
    strip._request_visible()
    assert str(target) not in strip._tasks and str(target) not in strip._video_pending
    strip.end_release(target)
    strip._request_visible()
    assert str(target) in strip._tasks or str(target) in strip._cache
    assert clip.exists()


def test_release_ends_a_running_video_thumbnail_grab(qapp, samples):
    from mediareaderx.thumbnails import VideoThumbnailer

    thumbs = VideoThumbnailer()
    failed = []
    thumbs.failed.connect(failed.append)
    clip = str(samples / "h264.mp4")
    thumbs.request(clip)
    assert thumbs.current == clip
    thumbs.release(clip)
    assert thumbs.current is None and failed == [clip]
