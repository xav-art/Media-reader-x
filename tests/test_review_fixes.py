"""Regression tests for the Fable 5.1 adversarial review findings (R1 to R21)."""

import shutil
import time
from pathlib import Path

import imagecodecs
import numpy as np
import pytest
from PIL import Image
from PySide6.QtCore import QCoreApplication, QElapsedTimer, QSettings, Qt
from PySide6.QtGui import QImage

from mediareaderx import config
from mediareaderx.cache import DecodedCache, cache_key
from mediareaderx.decoders import DecodeError, check_pixels, decode, jxl_size
from mediareaderx.folder import FolderNav, list_media
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


# -- R5: our own pixel limit, including a header-only JPEG XL size ---------------------------
@pytest.mark.parametrize("w, h", [(320, 240), (333, 111), (64, 64), (1000, 750), (17, 2001), (256, 144), (800, 600)])
def test_jxl_header_size(w, h):
    data = imagecodecs.jpegxl_encode(np.zeros((h, w, 3), np.uint8), level=50)
    assert jxl_size(data) == (w, h)


def test_jxl_size_in_container():
    raw = imagecodecs.jpegxl_encode(np.zeros((90, 160, 3), np.uint8), level=50)
    box = lambda kind, payload: (8 + len(payload)).to_bytes(4, "big") + kind + payload
    container = b"\x00\x00\x00\x0cJXL \x0d\x0a\x87\x0a" + box(b"ftyp", b"jxl \x00\x00\x00\x00jxl ") + box(b"jxlc", raw)
    assert jxl_size(container) == (160, 90)
    assert jxl_size(b"not a jxl") is None


def test_pixel_cap_is_enforced_below_pillows_2x(tmp_path, qapp, monkeypatch):
    monkeypatch.setattr(config, "MAX_IMAGE_PIXELS", 10_000)
    path = tmp_path / "big.png"
    Image.new("L", (150, 100)).save(path)  # 15 000 px: over our cap, under Pillow's 2x
    with pytest.raises(DecodeError, match="too large"):
        decode(path)
    with pytest.raises(DecodeError):
        check_pixels(200, 100)


# -- R1: a file that vanishes mid-decode gives a message, never a stuck "Loading" ---------------
def test_decode_maps_late_os_errors(tmp_path, qapp, monkeypatch):
    import mediareaderx.decoders as dec

    path = tmp_path / "x.png"
    Image.new("RGB", (8, 8)).save(path)
    monkeypatch.setattr(dec, "file_items", lambda p: (_ for _ in ()).throw(FileNotFoundError(p)))
    with pytest.raises(DecodeError, match="cannot be read"):
        decode(path)


def test_unexpected_worker_error_reaches_the_screen(window, folder, monkeypatch):
    import mediareaderx.main_window as mw

    monkeypatch.setattr(mw, "decode", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    window.open_path(folder / "sample.png")
    assert wait_until(lambda: window.stack.currentIndex() == window.PAGE_MESSAGE)
    assert "Open log folder" in window.message.text() and not window._inflight


# -- R6/R7: no stale queue, and an in-flight preload is reused --------------------------------
def test_rapid_navigation_shows_the_last_file(window, folder):
    images = [p for p in list_media(folder) if p.suffix.lower() in (".png", ".heic", ".avif", ".jxl", ".webp")]
    for p in images:
        window.open_path(p)
    assert wait_until(lambda: window._current_image is not None)
    assert window.current == images[-1]
    assert wait_until(lambda: not window._inflight)


def test_open_while_preloading_decodes_once(window, folder, monkeypatch):
    import mediareaderx.main_window as mw

    calls = []
    real = mw.decode

    def slow_decode(p, max_side=None):
        calls.append(Path(p).name)
        time.sleep(0.3)
        return real(p, max_side)

    monkeypatch.setattr(mw, "decode", slow_decode)
    files = list_media(folder)
    first = next(i for i, p in enumerate(files[:-1])
                 if p.suffix.lower() in (".png", ".heic", ".avif", ".jxl") and
                 files[i + 1].suffix.lower() in (".png", ".heic", ".avif", ".jxl"))
    window.open_path(files[first])
    nxt = files[first + 1]
    assert wait_until(lambda: str(nxt) in window._inflight and window._inflight[str(nxt)].started)
    window.open_path(nxt)
    assert wait_until(lambda: window._current_image is not None and window.current == nxt)
    assert calls.count(nxt.name) == 1


# -- R8: the slideshow skips a file that disappeared --------------------------------------------
def test_slideshow_continues_past_a_missing_file(window, folder, monkeypatch):
    monkeypatch.setattr(MainWindow, "slideshow_seconds", property(lambda self: 0))
    files = list_media(folder)
    window.open_path(files[0])
    files[1].unlink()
    window.act_slideshow.trigger()
    assert wait_until(lambda: window.current not in (files[0], files[1]) and window.current is not None, 10000)
    window.toggle_slideshow(False)


# -- R9: a damaged settings file never stops startup --------------------------------------------
def test_bad_settings_values_are_ignored(qapp, tmp_path):
    s = QSettings(str(config.settings_file()), QSettings.Format.IniFormat)
    for key, value in (("volume", "loud"), ("layout_version", "x"), ("slideshow_seconds", "soon")):
        s.setValue(key, value)
    s.sync()
    w = MainWindow()
    assert w.video_view.volume_percent == 80 and w.slideshow_seconds == 5
    w.close()


# -- R10/R11: full screen keeps the user's panels and the maximized state -----------------------
def test_full_screen_from_welcome_keeps_panels(window, folder):
    window.act_full.trigger()  # F11 on the welcome screen
    window.open_path(folder / "sample.png")
    window._on_escape()
    assert not window.meta_panel.isHidden() and not window.filmstrip.isHidden()


def test_full_screen_returns_to_maximized(window, folder):
    window.showMaximized()
    window.open_path(folder / "sample.png")
    window.toggle_fullscreen(True)
    window.toggle_fullscreen(False)
    assert window.windowState() & Qt.WindowState.WindowMaximized


# -- R12: the cache key is taken before decoding ------------------------------------------------
def test_cache_key_before_decode_prevents_stale_pixels(tmp_path, qapp):
    path = tmp_path / "x.png"
    Image.new("RGB", (10, 10), "red").save(path)
    key = cache_key(path)
    image = decode(path)
    time.sleep(0.02)
    Image.new("RGB", (20, 20), "blue").save(path)  # replaced while "decoding"
    cache = DecodedCache()
    cache.put(path, image, key)
    assert cache.get(path) is None  # the old pixels never serve the new file


# -- R14: stepping does not rescan the folder ----------------------------------------------------
def test_folder_listing_is_cached(folder, monkeypatch):
    import mediareaderx.folder as fmod

    files = list_media(folder)  # before counting: this call scans too
    scans = []
    real = fmod.scan
    monkeypatch.setattr(fmod, "scan", lambda f: scans.append(f) or real(f))
    nav = FolderNav()
    for p in files:
        nav.set_current(p)
    assert len(scans) == 1
    (folder / "new.png").write_bytes((folder / "sample.png").read_bytes())  # the folder changes
    nav.set_current(files[0])
    assert len(scans) == 2 and any(p.name == "new.png" for p in nav.files)


def test_live_photo_companion_from_listing(folder):
    nav = FolderNav()
    nav.set_current(folder / "IMG_0001.HEIC")
    assert nav.companion_of(folder / "IMG_0001.HEIC").name == "IMG_0001.MOV"
    assert nav.companion_of(folder / "sample.heic") is None


# -- R3/R15: shared placeholders and bounded thumbnail work --------------------------------------
def test_strip_placeholders_are_shared(window, tmp_path, samples):
    big = tmp_path / "many"
    big.mkdir()
    src = (samples / "sample.png").read_bytes()
    for i in range(400):
        (big / f"img_{i:04d}.png").write_bytes(src)
    window.filmstrip.hide()  # no thumbnail work: measure placeholders only
    window.open_path(big / "img_0000.png")
    keys = {window.filmstrip.list.item(i).icon().cacheKey() for i in range(window.filmstrip.list.count())}
    assert window.filmstrip.list.count() == 400 and len(keys) <= 2


# -- R4: network paths are not probed on the welcome screen --------------------------------------
def test_unc_paths_are_not_local():
    from mediareaderx.widgets.welcome import is_local_path

    assert not is_local_path(r"\\server\share\photo.heic")
    assert is_local_path(r"C:\Windows")


# -- R16 and R21 -----------------------------------------------------------------------------------
def test_pillow_decodes_from_memory(tmp_path, qapp, monkeypatch):
    import io

    import mediareaderx.decoders as dec

    path = tmp_path / "x.png"
    Image.new("RGB", (8, 8)).save(path)
    seen = []
    real_open = dec.Image.open
    monkeypatch.setattr(dec.Image, "open", lambda f, *a, **k: seen.append(type(f)) or real_open(f, *a, **k))
    decode(path)
    assert seen and all(t is io.BytesIO for t in seen)


def test_warm_up_runs(qapp):
    from mediareaderx.decoders import warm_up

    warm_up()
