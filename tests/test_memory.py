"""Tests for 1.3.1: screen-size decoding, full resolution on demand, and the memory fixes."""

import os
import shutil
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QCoreApplication, QElapsedTimer, QSize
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFileDialog

from mediareaderx.cache import DEFAULT_BUDGET_BYTES, budget_for_screen
from mediareaderx.decoders import decode
from mediareaderx.main_window import MainWindow, screen_side
from mediareaderx.widgets.image_view import ImageView


def wait_until(predicate, timeout_ms=8000):
    timer = QElapsedTimer()
    timer.start()
    while not predicate():
        if timer.elapsed() > timeout_ms:
            return False
        QCoreApplication.processEvents()
    return True


def _dims(image) -> str:
    return next(m.value for m in image.metadata if m.key == "Dimensions")


def test_openblas_is_limited_before_numpy_loads():
    import mediareaderx  # noqa: F401

    assert os.environ.get("OPENBLAS_NUM_THREADS") == "1"


@pytest.mark.parametrize("name", ["sample.png", "sample.heic", "sample.avif", "sample.jxl"])
def test_screen_copy_keeps_the_real_size_in_metadata(samples, qapp, name):
    path = samples / name
    full = decode(path)
    small = decode(path, 64)
    assert small.reduced and not full.reduced
    assert max(small.frames[0].width(), small.frames[0].height()) == 64
    assert small.size == full.size == full.frames[0].size()
    assert _dims(small) == _dims(full)


def test_no_reduction_when_the_picture_already_fits(samples, qapp):
    img = decode(samples / "sample.png", 5000)
    assert not img.reduced and img.size == img.frames[0].size()


def test_animations_keep_full_size(samples, qapp):
    img = decode(samples / "anim.webp", 16)
    assert img.animated and not img.reduced


def test_jpeg_with_exif_rotation_is_upright_and_reports_upright_size(tmp_path, qapp):
    path = tmp_path / "turned.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6  # stored landscape, shown portrait
    Image.new("RGB", (800, 400), (200, 30, 30)).save(path, exif=exif.tobytes())
    small = decode(path, 100)
    assert small.size == QSize(400, 800)
    assert small.frames[0].height() == 100 and small.frames[0].width() == 50


def test_large_jpeg_uses_dct_scaling(tmp_path, qapp):
    path = tmp_path / "big.jpg"
    Image.new("RGB", (4000, 3000), (10, 120, 200)).save(path, quality=80)
    small = decode(path, 500)
    assert (small.frames[0].width(), small.frames[0].height()) == (500, 375)
    assert small.size == QSize(4000, 3000)


def test_raw_uses_half_size_only_when_it_still_covers_the_screen(monkeypatch, qapp, tmp_path):
    import types

    import numpy as np
    import rawpy

    seen = []

    class FakeRaw:
        sizes = types.SimpleNamespace(width=6000, height=4000, raw_width=6048, raw_height=4024, flip=6)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def postprocess(self, **kw):
            seen.append(kw["half_size"])
            h, w = (3000, 2000) if kw["half_size"] else (6000, 4000)  # flip 6: upright portrait
            return np.zeros((h, w, 3), np.uint8)

    monkeypatch.setattr(rawpy, "imread", lambda p: FakeRaw())
    path = tmp_path / "x.nef"
    path.write_bytes(b"raw")
    small = decode(path, 2560)
    full = decode(path, 4000)
    assert seen == [True, False]
    assert small.size == full.size == QSize(4000, 6000)
    assert small.frames[0].height() == 2560


def test_cache_budget_follows_the_screen():
    assert DEFAULT_BUDGET_BYTES <= 192 * 1024**2
    one_2560 = 2560 * 1920 * 4
    assert budget_for_screen(2560, 5) == 5 * one_2560  # about 98 MB
    assert budget_for_screen(3840, 5) > 4 * 3840 * 2880 * 4  # 4K: still room for every neighbour


def test_screen_side_covers_the_largest_screen(qapp):
    side = screen_side()
    for s in QGuiApplication.screens():
        assert side >= max(s.size().width(), s.size().height()) * s.devicePixelRatio() - 1


# -- the viewer ---------------------------------------------------------------------
def test_view_shows_a_screen_copy_at_real_scale_and_asks_for_detail_once(samples, qapp):
    view = ImageView()
    view.resize(50, 40)  # fit zoom about 0.15: below what the 64 px copy holds (0.2)
    view.show()
    small = decode(samples / "sample.png", 64)
    asked = []
    view.detailNeeded.connect(lambda: asked.append(1))
    view.set_image(small)
    assert view.sceneRect().width() == pytest.approx(small.size.width())
    assert not asked  # fit to a small window: the copy is enough
    view.actual_size()
    view.zoom_by(1.25)
    assert asked == [1]
    view.rotate_by(90)
    view.upgrade(decode(samples / "sample.png"))
    assert not view.reduced and view.rotation == 90
    assert view.sceneRect().width() == pytest.approx(small.size.height())  # rotated, same real size
    assert view.current_frame().size() == QSize(small.size.height(), small.size.width())


def test_upgrade_ignores_a_different_picture(samples, qapp):
    view = ImageView()
    view.set_image(decode(samples / "sample.png", 64))
    view.upgrade(decode(samples / "sample.svg"))
    assert view.reduced


# -- the window ---------------------------------------------------------------------
@pytest.fixture
def folder(samples, tmp_path):
    dest = tmp_path / "album"
    shutil.copytree(samples, dest)
    return dest


@pytest.fixture
def window(qapp):
    w = MainWindow()
    w._max_side = 64  # make every sample a "large" photo
    w.show()
    yield w
    w.close()


def _open(window, path):
    window.open_path(path)
    assert wait_until(lambda: window._current_image is not None and window.current == path)


def test_window_shows_screen_copies_and_reports_the_real_size(window, folder):
    _open(window, folder / "sample.png")
    assert window._current_image.reduced
    assert "320 x 240" in window.status.text()
    assert window._cache.get(folder / "sample.png").reduced  # the cache holds screen copies


def test_zoom_decodes_full_resolution_and_navigation_drops_it(window, folder):
    _open(window, folder / "sample.png")
    window.image_view.actual_size()
    assert wait_until(lambda: window._full_image is not None)
    assert not window.image_view.reduced
    assert window._cache.get(folder / "sample.png").reduced  # never cached at full size
    _open(window, folder / "sample.heic")
    assert window._full_image is None


def test_copy_and_save_use_full_resolution(window, folder, monkeypatch):
    _open(window, folder / "sample.png")
    window.copy()
    assert QGuiApplication.clipboard().image().size() == QSize(320, 240)
    target = folder.parent / "copy.png"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), "PNG image (*.png)"))
    window.save_copy()
    with Image.open(target) as im:
        assert im.size == (320, 240)


def test_save_cancel_does_not_decode_full(window, folder, monkeypatch):
    _open(window, folder / "sample.png")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: ("", ""))
    window.save_copy()
    assert window._full_image is None


def test_late_full_decode_for_an_old_photo_is_ignored(window, folder):
    _open(window, folder / "sample.png")
    window.image_view.actual_size()  # starts the full decode
    _open(window, folder / "sample.heic")  # moves on before it arrives
    assert wait_until(lambda: window._pool.activeThreadCount() == 0)
    QCoreApplication.processEvents()
    # The HEIC may get its own full decode (it fits the test window at 100%); the PNG's never lands.
    assert window._full_image is None or window._full_image.format_name != "PNG"
    assert window.current == folder / "sample.heic"


# -- optimisation review fixes ------------------------------------------------------------
def _jpeg_with_preview(path, preview_size, orientation=1, main=(4000, 3000)):
    """A red JPEG whose EXIF block carries a green preview, laid out as a camera writes it.

    Pillow cannot write IFD1 (preview) tags, so the little-endian TIFF block is built by hand:
    IFD0 with the orientation, IFD1 with the preview's offset and length, then the preview.
    """
    import io
    import struct

    buf = io.BytesIO()
    Image.new("RGB", preview_size, (0, 200, 0)).save(buf, "JPEG")
    preview = buf.getvalue()
    ifd0_at, ifd1_at = 8, 8 + 2 + 12 + 4
    data_at = ifd1_at + 2 + 2 * 12 + 4
    tiff = b"II*\x00" + struct.pack("<I", ifd0_at)
    tiff += struct.pack("<H", 1) + struct.pack("<HHIHH", 0x0112, 3, 1, orientation, 0) + struct.pack("<I", ifd1_at)
    tiff += struct.pack("<H", 2) + struct.pack("<HHII", 0x0201, 4, 1, data_at)
    tiff += struct.pack("<HHII", 0x0202, 4, 1, len(preview)) + struct.pack("<I", 0)
    Image.new("RGB", main, (200, 0, 0)).save(path, "JPEG", exif=b"Exif\x00\x00" + tiff + preview)
    return path

def test_strip_uses_the_jpeg_exif_preview(tmp_path, qapp):
    from mediareaderx import thumbnails

    path = _jpeg_with_preview(tmp_path / "phone.jpg", (320, 240))
    assert thumbnails._exif_thumbnail(path, 192) is not None
    tile = thumbnails.make_thumbnail(path)
    assert tile.pixelColor(tile.width() // 2, tile.height() // 2).green() > 150  # the preview, not the photo


def test_strip_turns_the_exif_preview_like_the_photo(tmp_path, qapp):
    from mediareaderx import thumbnails

    path = _jpeg_with_preview(tmp_path / "turned.jpg", (320, 240), orientation=6)
    tile = thumbnails._exif_thumbnail(path, 192)
    assert tile.height() > tile.width()


def test_strip_skips_a_small_exif_preview(tmp_path, qapp):
    from mediareaderx import thumbnails

    path = _jpeg_with_preview(tmp_path / "old.jpg", (160, 120))
    assert thumbnails._exif_thumbnail(path, 192) is None
    tile = thumbnails.make_thumbnail(path)
    assert tile.pixelColor(tile.width() // 2, tile.height() // 2).red() > 150  # decoded from the photo


def test_srgb_profiles_skip_the_colour_transform(qapp):
    import io

    from PIL import ImageCms

    from mediareaderx import decoders

    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    assert decoders._is_srgb(srgb)
    assert not decoders._is_srgb(b"not a profile")
    im = Image.new("RGB", (4, 4), (10, 20, 30))
    im.info["icc_profile"] = srgb
    assert decoders._to_srgb(im) is im  # untouched: no conversion work
    del io


def test_screen_copy_colours_survive_the_faster_shrink(tmp_path, qapp):
    path = tmp_path / "flat.png"
    Image.new("RGB", (3000, 2000), (12, 200, 99)).save(path)
    small = decode(path, 500)
    c = small.frames[0].pixelColor(250, 166)
    assert (c.red(), c.green(), c.blue()) == (12, 200, 99)
    assert small.frames[0].width() == 500


def test_rgba_photo_keeps_its_alpha_in_the_screen_copy(tmp_path, qapp):
    path = tmp_path / "alpha.png"
    Image.new("RGBA", (1000, 500), (255, 0, 0, 0)).save(path)
    small = decode(path, 100)
    assert small.frames[0].hasAlphaChannel()
    assert small.frames[0].pixelColor(50, 25).alpha() == 0


def test_small_zoom_past_the_copy_does_not_decode_full(samples, qapp):
    from mediareaderx.widgets.image_view import DETAIL_TOLERANCE

    view = ImageView()
    view.resize(50, 40)
    view.show()
    small = decode(samples / "sample.png", 64)
    asked = []
    view.detailNeeded.connect(lambda: asked.append(1))
    view.set_image(small)
    copy_zoom = 64 / 320
    view.zoom_by(copy_zoom * 1.1 / view.zoom)  # 10% past the copy: still sharp enough
    assert not asked
    view.zoom_by(copy_zoom * (DETAIL_TOLERANCE + 0.1) / view.zoom)
    assert asked == [1]


def test_stepping_in_one_folder_does_not_rewrite_last_dir(window, folder, monkeypatch):
    _open(window, folder / "sample.png")
    writes = []
    real = window.settings.setValue
    monkeypatch.setattr(window.settings, "setValue", lambda k, v: (writes.append(k), real(k, v)))
    _open(window, folder / "sample.heic")
    assert "last_dir" not in writes


# -- 1.4.1: MPO (iPhone HDR JPEG with a gain map) -------------------------------------------
def _mpo(path, orientation=6):
    """A two-picture JPEG like an iPhone HDR photo: the red photo, then a grey half-size gain map."""
    exif = Image.Exif()
    exif[0x0112] = orientation
    photo = Image.new("RGB", (800, 600), (200, 30, 30))
    gain = Image.new("L", (400, 300), 128)
    photo.save(path, "MPO", save_all=True, append_images=[gain], exif=exif.tobytes())
    return path


def test_mpo_shows_one_upright_still_photo(tmp_path, qapp):
    path = _mpo(tmp_path / "IMG_0001.jpeg")
    with Image.open(path) as im:
        assert im.format == "MPO" and im.is_animated  # what made the app play it as an animation
    img = decode(path)
    assert not img.animated and len(img.frames) == 1
    assert img.size == QSize(600, 800)  # turned upright by the EXIF orientation
    assert img.frames[0].pixelColor(300, 400).red() > 180  # the photo, not the grey gain map
    assert img.format_name == "JPEG (MPO)"


def test_mpo_gets_a_screen_copy(tmp_path, qapp):
    img = decode(_mpo(tmp_path / "IMG_0002.jpeg"), 200)
    assert img.reduced and img.frames[0].size() == QSize(150, 200) and img.size == QSize(600, 800)


def test_mpo_strip_tile_is_upright(tmp_path, qapp):
    from mediareaderx.thumbnails import make_thumbnail

    tile = make_thumbnail(_mpo(tmp_path / "IMG_0003.jpeg"))
    assert tile.height() > tile.width()
    assert tile.pixelColor(tile.width() // 2, tile.height() // 2).red() > 180
