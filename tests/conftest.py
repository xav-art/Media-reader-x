"""Shared fixtures: synthetic samples and an offscreen Qt application."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def samples(tmp_path_factory):
    import make_samples

    out = tmp_path_factory.mktemp("samples")
    make_samples.make_images(out)
    make_samples.make_videos(out)
    return out


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _no_blocking_message_boxes(monkeypatch):
    """A modal message box waits for a click that never comes on the offscreen platform: the run hangs.

    Record each one and return at once instead, then fail the test with the box's text, so an unexpected
    dialog is a clear failure, never a hang. A test that expects a box can read and clear ``boxes``.
    """
    from PySide6.QtWidgets import QMessageBox

    boxes = []
    for name in ("warning", "critical", "information", "question", "about"):
        def record(parent, title, text, *args, _name=name, **kwargs):
            boxes.append((_name, title, text))
            return QMessageBox.StandardButton.Ok
        monkeypatch.setattr(QMessageBox, name, staticmethod(record))
    yield boxes
    if boxes:
        pytest.fail(f"unexpected message box: {boxes}")


@pytest.fixture(autouse=True)
def _isolated_appdata(tmp_path, monkeypatch):
    """Keep settings and logs out of the real %LOCALAPPDATA% during tests."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
