"""Tests for 1.1: folder navigation, thumbnails, export, delete, copy, recent, theme, video speed/loop."""

import shutil
from fractions import Fraction
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QCoreApplication, QElapsedTimer, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QFileDialog

from mediareaderx import config
from mediareaderx.export import ExportError, export_image
from mediareaderx.folder import FolderNav, list_media, natural_key
from mediareaderx.main_window import MainWindow
from mediareaderx.metadata import GPS_IFD
from mediareaderx.thumbnails import THUMB_SIDE, VideoThumbnailer, make_thumbnail
from mediareaderx.decoders import decode


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
    """A private copy of the samples, safe to delete from."""
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


# -- folder ----------------------------------------------------------------------
def test_natural_sort_matches_explorer():
    names = ["IMG_10.heic", "img_2.HEIC", "IMG_1.heic"]
    assert [p.name for p in sorted(map(Path, names), key=natural_key)] == ["IMG_1.heic", "img_2.HEIC", "IMG_10.heic"]


def test_live_photo_video_is_hidden_from_listing(folder):
    names = [p.name for p in list_media(folder)]
    assert "IMG_0001.HEIC" in names and "IMG_0001.MOV" not in names
    assert "hevc.mov" in names and "sample.svg" in names


def test_folder_nav_steps_and_bounds(folder):
    nav = FolderNav()
    files = list_media(folder)
    nav.set_current(files[0])
    assert nav.step(-1) is None and nav.first() is None
    assert nav.step(1) == files[1]
    assert nav.last() == files[-1]
    assert nav.position_text == f"1 / {len(files)}"


def test_folder_nav_remove_current_moves_to_next(folder):
    nav = FolderNav()
    files = list_media(folder)
    nav.set_current(files[-1])
    assert nav.remove_current() == files[-2]  # last item removed: falls back to previous


# -- thumbnails ------------------------------------------------------------------
@pytest.mark.parametrize("name", ["sample.heic", "sample.avif", "sample.jxl", "sample.svg", "anim.gif"])
def test_thumbnails_are_small(samples, qapp, name):
    img = make_thumbnail(samples / name)
    assert img is not None and 0 < max(img.width(), img.height()) <= THUMB_SIDE


def test_thumbnail_of_broken_file_is_none(tmp_path, qapp):
    bad = tmp_path / "bad.heic"
    bad.write_bytes(b"nope" * 100)
    assert make_thumbnail(bad) is None


def test_video_thumbnailer_grabs_a_frame(samples, qapp):
    got = {}
    thumbs = VideoThumbnailer()
    thumbs.ready.connect(lambda path, img: got.setdefault(path, img))
    thumbs.request(str(samples / "hevc.mp4"))
    assert wait_until(lambda: got)
    img = next(iter(got.values()))
    assert max(img.width(), img.height()) <= THUMB_SIDE


# -- export ----------------------------------------------------------------------
def _heic_with_gps(path: Path) -> None:
    import pillow_heif

    pillow_heif.register_heif_opener()
    exif = Image.Exif()
    exif[0x010F] = "SampleMake"
    gps = exif.get_ifd(GPS_IFD)
    gps.update({1: "N", 2: (Fraction(55), Fraction(40), Fraction(0)), 3: "E", 4: (Fraction(12), Fraction(0), Fraction(0))})
    Image.new("RGB", (64, 32), "teal").save(path, exif=exif.tobytes())


def test_export_jpg_keeps_exif_and_can_strip_gps(tmp_path, qapp):
    src = tmp_path / "photo.heic"
    _heic_with_gps(src)
    img = decode(src)
    keep = export_image(img.frames[0], tmp_path / "keep.jpg", source=src, exif_bytes=img.exif_bytes)
    strip = export_image(img.frames[0], tmp_path / "strip.jpg", source=src, exif_bytes=img.exif_bytes,
                         strip_gps=True)
    with Image.open(keep) as k, Image.open(strip) as s:
        assert k.format == "JPEG" and k.size == (64, 32)
        assert k.getexif()[0x010F] == "SampleMake" and k.getexif().get_ifd(GPS_IFD)
        assert s.getexif()[0x010F] == "SampleMake" and not s.getexif().get_ifd(GPS_IFD)


def test_export_applies_rotation_and_png(samples, tmp_path, qapp):
    img = decode(samples / "sample.png")
    out = export_image(img.frames[0], tmp_path / "rot.png", rotation=90)
    with Image.open(out) as im:
        assert im.size == (240, 320)


def test_export_never_overwrites_source(samples, qapp):
    img = decode(samples / "sample.png")
    with pytest.raises(ExportError, match="never overwrites"):
        export_image(img.frames[0], samples / "sample.png", source=samples / "sample.png")


# -- settings migration ----------------------------------------------------------
def test_legacy_settings_are_imported_once(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    legacy = tmp_path / "MediaReader"
    legacy.mkdir()
    (legacy / "settings.ini").write_text("[General]\nvolume=33\n", encoding="utf-8")
    assert config.migrate_legacy_settings() is True
    assert "volume=33" in config.settings_file().read_text(encoding="utf-8")
    assert config.migrate_legacy_settings() is False


# -- main window -------------------------------------------------------------------
def test_arrow_navigation_and_strip(window, folder):
    files = list_media(folder)
    window.open_path(files[0])
    assert window.filmstrip.list.count() == len(files)
    window.act_next.trigger()
    assert window.current == files[1]
    assert window.status.text().startswith("2 /") or wait_until(lambda: window.status.text().startswith("2 /"))
    window.act_last.trigger()
    assert window.current == files[-1] and not window.act_next.isEnabled()
    window.act_first.trigger()
    assert window.current == files[0]


def test_rotate_and_copy_picture(window, folder):
    window.open_path(folder / "sample.png")
    assert wait_until(lambda: _image_shown(window))
    window.act_rot_right.trigger()
    assert window.image_view.rotation == 90
    window.act_copy.trigger()
    clip = QGuiApplication.clipboard().image()
    assert (clip.width(), clip.height()) == (240, 320)
    window.act_copy_path.trigger()
    assert QGuiApplication.clipboard().text() == str(folder / "sample.png")


def test_delete_moves_to_recycle_bin_and_shows_next(window, folder):
    files = list_media(folder)
    target = files[0]
    window.open_path(target)
    window.act_delete.trigger()
    assert not target.exists()
    assert window.current == files[1]
    assert window.filmstrip.list.count() == len(files) - 1


def test_delete_live_photo_takes_its_video(window, folder):
    window.open_path(folder / "IMG_0001.HEIC")
    assert wait_until(lambda: window.companion is not None)
    window.act_delete.trigger()
    assert not (folder / "IMG_0001.HEIC").exists() and not (folder / "IMG_0001.MOV").exists()


def test_recent_files_newest_first_without_duplicates(window, folder):
    window.open_path(folder / "sample.png")
    window.open_path(folder / "sample.avif")
    window.open_path(folder / "sample.png")
    recent = window._recent()
    assert recent[:2] == [str(folder / "sample.png"), str(folder / "sample.avif")]


def test_save_copy_from_window(window, folder, monkeypatch):
    window.open_path(folder / "sample.heic")
    assert wait_until(lambda: _image_shown(window))
    dest = folder / "shared.jpg"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(dest), "JPEG image (*.jpg)"))
    window.act_save.trigger()
    assert dest.is_file()
    assert any(p.name == "shared.jpg" for p in window.nav.files)


def test_theme_switch(window):
    # The headless "offscreen" platform ignores colour schemes; real Windows honours them.
    real_display = QGuiApplication.platformName() != "offscreen"
    window.set_theme("dark")
    if real_display:
        assert QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    window.set_theme("light")
    if real_display:
        assert QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Light
    assert window.theme == "light" and window.settings.value("theme") == "light"
    window.set_theme("bogus")
    assert window.theme == "system"


def test_video_speed_and_loop(window, folder):
    window.open_path(folder / "hevc.mp4")
    view = window.video_view
    window.act_faster.trigger()
    assert view.rate == 1.25 and view.speed_btn.text() == "1.25x"
    window.act_slower.trigger()
    window.act_slower.trigger()
    assert view.rate == 0.75
    window.act_loop.trigger()
    assert view.loop_btn.isChecked() and view.player.loops() == -1
    window.open_path(folder / "h264.mp4")
    assert view.rate == 1.0 and view.player.loops() == -1  # loop preference persists, speed resets


# -- UI review fixes ---------------------------------------------------------------
def test_toolbar_is_icon_only_with_shortcut_tooltips(window):
    from PySide6.QtWidgets import QToolBar

    toolbar = window.findChild(QToolBar, "MainToolbar")
    assert toolbar.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
    for act in toolbar.actions():
        if not act.isSeparator():
            assert not act.icon().isNull(), act.text()
    assert window.act_next.toolTip() == "Next file (Right)"
    assert window.act_meta.toolTip() == "Metadata panel (I)"


def test_image_tools_hidden_for_video(window, folder):
    window.open_path(folder / "hevc.mp4")
    assert not window.act_zoom_in.isVisible() and not window.act_rot_left.isVisible()
    window.open_path(folder / "sample.png")
    assert wait_until(lambda: _image_shown(window))
    assert window.act_zoom_in.isVisible() and window.act_rot_left.isVisible()


def test_brand_dark_palette(window):
    from PySide6.QtGui import QPalette
    from mediareaderx.theme import CORAL

    window.set_theme("dark")
    pal = QGuiApplication.palette()
    assert pal.color(QPalette.ColorRole.Window).name() == "#171e31"
    assert pal.color(QPalette.ColorRole.Highlight).name() == CORAL.lower()
    window.set_theme("light")
    assert QGuiApplication.palette().color(QPalette.ColorRole.Window).name() != "#171e31"
    window.set_theme("system")


def test_strip_and_panel_layout(window, folder):
    window.open_path(folder / "sample.png")
    assert window.filmstrip.titleBarWidget() is not None  # empty widget = no title bar
    assert "QListWidget::item:selected" in window.filmstrip.list.styleSheet()
    assert window.meta_panel.tree.textElideMode() == Qt.TextElideMode.ElideMiddle
    assert wait_until(lambda: abs(window.meta_panel.width() - 300) <= 40, 3000)


def test_decoder_crash_message_tells_where_to_look(tmp_path, qapp, monkeypatch):
    import mediareaderx.decoders as dec

    monkeypatch.setattr(dec, "_decode_pillow", lambda _p: 1 / 0)
    path = tmp_path / "x.png"
    Image.new("RGB", (4, 4)).save(path)
    with pytest.raises(dec.DecodeError, match=r"Help > Open log folder"):
        dec.decode(path)


def test_icons_follow_the_palette(window):
    """Regression: light theme showed pale icons tinted for dark mode."""
    from PySide6.QtGui import QPalette

    def icon_color():
        img = window.act_open.icon().pixmap(48, 48).toImage()
        opaque = [img.pixelColor(x, y) for x in range(48) for y in range(48) if img.pixelColor(x, y).alpha() > 200]
        return max(opaque, key=lambda c: c.alpha())

    window.set_theme("dark")
    assert icon_color().lightness() > 180
    window.set_theme("light")
    expected = QApplication.palette().color(QPalette.ColorRole.ButtonText)
    assert abs(icon_color().lightness() - expected.lightness()) < 40
    window.set_theme("system")


def test_first_start_window_fits_the_screen(window):
    """Regression: a fixed 1200 x 820 window ran under the taskbar at 150% scaling."""
    window.settings.remove("geometry")
    window._fit_to_screen()
    avail = window.screen().availableGeometry()
    assert window.width() <= avail.width() and window.height() <= avail.height()
    assert window.width() <= 1200 and window.height() <= 820
