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
def _isolated_appdata(tmp_path, monkeypatch):
    """Keep settings and logs out of the real %LOCALAPPDATA% during tests."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
