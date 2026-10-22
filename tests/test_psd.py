"""Tests for 1.4: Photoshop PSD and PSB files through psd-tools."""

import shutil
import sys
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QSize

from mediareaderx.decoders import NO_MERGED_IMAGE, DecodeError, decode, psd_header
from mediareaderx.formats import ASSOCIATION_EXTS, MediaKind, kind_of
from mediareaderx.thumbnails import make_thumbnail

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from make_samples import _make_psd  # noqa: E402


def _base(size=(320, 240), color=(200, 30, 30)) -> Image.Image:
    return Image.new("RGB", size, color)


def _meta(image, key):
    return next(m.value for m in image.metadata if m.key == key)


def test_psd_and_psb_are_supported_and_associated():
    for ext in (".psd", ".psb"):
        assert kind_of(f"x{ext}") is MediaKind.IMAGE
        assert ext in ASSOCIATION_EXTS


@pytest.mark.parametrize("depth", [8, 16, 32])
def test_layered_psd_shows_the_flattened_image(tmp_path, qapp, depth):
    path = tmp_path / f"art{depth}.psd"
    _make_psd(path, _base(), depth=depth)
    img = decode(path)
    assert img.size == QSize(320, 240)
    assert img.frames[0].pixelColor(200, 200).red() > 180  # the photo layer
    assert img.frames[0].pixelColor(20, 20).name() == "#fafa14"  # the box layer on top
    assert _meta(img, "Colour mode") == f"RGB, {depth}-bit"
    assert _meta(img, "Layers") == "2"
    assert img.format_name == "Photoshop (PSD)"


def test_sample_psd_is_16_bit(samples, qapp):
    assert psd_header(samples / "sample.psd")[2] == 16
    assert decode(samples / "sample.psd").size == QSize(320, 240)


def test_psb_opens(tmp_path, qapp):
    from psd_tools import PSDImage

    import attrs

    psd = PSDImage.new("RGB", (300, 200), color=(10, 120, 200))
    psd.create_pixel_layer(_base((300, 200), (10, 120, 200)))
    # psd-tools writes PSB only above 30,000 px; switch the header to version 2 to keep the test small.
    psd._record.header = attrs.evolve(psd._record.header, version=2)
    path = tmp_path / "large.psb"
    psd.save(path)
    assert path.read_bytes()[4:6] == b"\x00\x02"
    assert psd_header(path) == (300, 200, 8, 3)
    img = decode(path)
    assert img.format_name == "Photoshop (PSB)"
    assert img.frames[0].pixelColor(150, 100).name() == "#0a78c8"


def test_psd_screen_copy_keeps_the_real_size(tmp_path, qapp):
    path = tmp_path / "big.psd"
    _make_psd(path, _base((2000, 1000)))
    img = decode(path, 500)
    assert img.reduced and img.size == QSize(2000, 1000)
    assert img.frames[0].size() == QSize(500, 250)
    assert "2000 x 1000" in _meta(img, "Dimensions")


def test_psd_without_a_flattened_image_explains_the_fix(tmp_path, qapp):
    path = tmp_path / "nomerge.psd"
    _make_psd(path, _base(), merged=False)
    with pytest.raises(DecodeError) as err:
        decode(path)
    assert str(err.value) == NO_MERGED_IMAGE
    assert "Maximize PSD and PSB File Compatibility" in NO_MERGED_IMAGE


def test_damaged_and_fake_psd_files_give_plain_errors(tmp_path, qapp):
    fake = tmp_path / "fake.psd"
    fake.write_bytes(b"not a photoshop file at all, just text" * 4)
    with pytest.raises(DecodeError, match="not a Photoshop file"):
        decode(fake)
    good = tmp_path / "good.psd"
    _make_psd(good, _base())
    cut = tmp_path / "cut.psd"
    cut.write_bytes(good.read_bytes()[:400])
    with pytest.raises(DecodeError):
        decode(cut)


def test_psd_over_the_pixel_limit_is_refused_before_parsing(tmp_path, qapp, monkeypatch):
    import mediareaderx.decoders as dec
    from mediareaderx import config

    path = tmp_path / "huge.psd"
    _make_psd(path, _base())
    monkeypatch.setattr(config, "MAX_IMAGE_PIXELS", 1000)
    parsed = []
    monkeypatch.setattr("psd_tools.PSDImage.open", lambda *a, **k: parsed.append(1))
    with pytest.raises(DecodeError, match="too large"):
        dec.decode(path)
    assert not parsed


def test_strip_tile_for_psd(tmp_path, qapp):
    path = tmp_path / "tile.psd"
    _make_psd(path, _base((800, 600)))
    tile = make_thumbnail(path)
    assert tile is not None and max(tile.width(), tile.height()) == 192
    assert tile.pixelColor(150, 120).red() > 180


def test_strip_uses_photoshops_own_preview(tmp_path, qapp, monkeypatch):
    from psd_tools import PSDImage

    path = tmp_path / "pv.psd"
    _make_psd(path, _base((800, 600)))
    flattened = []
    monkeypatch.setattr(PSDImage, "thumbnail", lambda self: Image.new("RGB", (160, 120), (0, 0, 255)))
    real_topil = PSDImage.topil
    monkeypatch.setattr(PSDImage, "topil", lambda self, *a, **k: (flattened.append(1), real_topil(self))[1])
    tile = make_thumbnail(path)
    assert tile.pixelColor(80, 60).blue() > 200 and not flattened  # the preview, no flattening


def test_window_opens_a_psd(samples, tmp_path, qapp):
    from PySide6.QtCore import QCoreApplication, QElapsedTimer

    from mediareaderx.main_window import MainWindow

    album = tmp_path / "album"
    shutil.copytree(samples, album)
    w = MainWindow()
    w.show()
    try:
        w.open_path(album / "sample.psd")
        timer = QElapsedTimer()
        timer.start()
        while w._current_image is None and timer.elapsed() < 8000:
            QCoreApplication.processEvents()
        assert w._current_image is not None
        assert "Photoshop (PSD)" in w.status.text() and "320 x 240" in w.status.text()
    finally:
        w.close()
