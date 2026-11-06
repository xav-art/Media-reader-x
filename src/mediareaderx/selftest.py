"""Headless health check: ``MediaReaderX.exe --self-test``.

Decodes tiny images with each bundled codec (HEIC and AVIF from committed probe
files, so no HEVC or AV1 encoder runs), confirms Qt's FFmpeg multimedia backend
loads, and checks that the decoder lock, video container check and SVG picture
check refuse crafted files. It also starts a protected helper (sandbox.py), decodes with it and checks
that it cannot read files, use the network or start programs. Results go to the log and to
%LOCALAPPDATA%\\MediaReaderX\\selftest.txt; the exit code is 0 only if all pass.
Used by build.ps1 against the frozen app, and useful for IT support.
"""

from __future__ import annotations

import io
import logging
import tempfile
from pathlib import Path

from mediareaderx import __version__
from mediareaderx.config import data_dir

log = logging.getLogger(__name__)


def _roundtrip(ext: str, save) -> None:
    from PIL import Image

    from mediareaderx.decoders import decode

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"probe{ext}"
        save(Image.new("RGB", (16, 8), (200, 50, 50)), path)
        img = decode(path)
        if (img.size.width(), img.size.height()) != (16, 8):
            raise AssertionError(f"unexpected size {img.size}")


def _decode_probe(name: str) -> None:
    """Decode a committed probe file: the app never runs the bundled HEVC (x265) or AV1 encoders."""
    from mediareaderx.decoders import decode
    from mediareaderx.resources import resource_dir

    img = decode(resource_dir() / name)
    if (img.size.width(), img.size.height()) != (16, 8):
        raise AssertionError(f"unexpected size {img.size}")
    red = (img.frames[0].pixel(8, 4) >> 16) & 0xFF
    if not 180 <= red <= 220:  # the probe is RGB (200, 50, 50); lossy coding moves it a little
        raise AssertionError(f"unexpected colour {img.frames[0].pixel(8, 4):#x}")


def _check_heic():
    _decode_probe("probe.heic")


def _check_avif():
    _decode_probe("probe.avif")


def _expect_refused(fn, *args) -> None:
    from mediareaderx.content import Refused
    from mediareaderx.decoders import DecodeError

    try:
        fn(*args)
    except (Refused, DecodeError):
        return
    raise AssertionError(f"{getattr(fn, '__name__', fn)} accepted a file it must refuse")


def _check_image_lock():
    import pillow_heif
    from PIL import Image

    from mediareaderx.decoders import decode, register_plugins

    register_plugins()
    if "HEIF" in Image.SAVE or "HEIF" in Image.SAVE_ALL:
        raise AssertionError("the HEIF writer (x265) is registered")
    if pillow_heif.options.DISABLE_SECURITY_LIMITS:
        raise AssertionError("libheif security limits are off")
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "png.heic"
        Image.new("RGB", (4, 4), (0, 0, 255)).save(fake, "PNG")
        img = decode(fake)  # opens with the PNG decoder only, and says so
        if img.format_name != "PNG" or not img.notice:
            raise AssertionError(f"PNG named .heic: {img.format_name}, notice {img.notice!r}")
        program = Path(tmp) / "program.heic"
        program.write_bytes(b"MZ" + bytes(510))
        _expect_refused(decode, program)


def _check_video_lock():
    import os

    from mediareaderx.content import check_video

    if os.environ.get("QT_FFMPEG_PROTOCOL_WHITELIST") != "file":
        raise AssertionError("FFmpeg may open protocols other than local files")
    with tempfile.TemporaryDirectory() as tmp:
        for name, data in (("png.mp4", b"\x89PNG\r\n\x1a\n" + bytes(64)),
                           ("playlist.mp4", b"#EXTM3U\n#EXT-X-VERSION:3\nhttp://example.invalid/a.ts\n")):
            path = Path(tmp) / name
            path.write_bytes(data)
            _expect_refused(check_video, path)
        ok = Path(tmp) / "ok.mp4"
        ok.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + bytes(64))
        check_video(ok)


def _check_svg_lock():
    from mediareaderx.content import load_svg

    head = '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="4" height="4">'
    with tempfile.TemporaryDirectory() as tmp:
        linked = Path(tmp) / "linked.svg"
        linked.write_text(head + '<image href="C:/Windows/Web/Wallpaper/x.jpg" width="4" height="4"/></svg>')
        _expect_refused(load_svg, linked)
        tiff = Path(tmp) / "tiff.svg"
        tiff.write_text(head + '<image xlink:href="data:image/png;base64,SUkqAAgAAAA=" width="4" height="4"/></svg>')
        _expect_refused(load_svg, tiff)
        plain = Path(tmp) / "plain.svg"
        plain.write_text(head + '<rect width="4" height="4"/></svg>')
        load_svg(plain)
        # An embedded JPEG must still draw: proves Qt's JPEG plugin survived the bundle trim.
        import base64

        from PIL import Image

        from mediareaderx.decoders import decode

        buf = io.BytesIO()
        Image.new("RGB", (8, 8), (220, 30, 30)).save(buf, "JPEG", quality=95)
        jpeg = Path(tmp) / "jpeg.svg"
        jpeg.write_text(head + '<image href="data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode()
                        + '" width="4" height="4"/></svg>')
        img = decode(jpeg)
        if (img.frames[0].pixel(img.frames[0].width() // 2, img.frames[0].height() // 2) >> 16) & 0xFF < 180:
            raise AssertionError("an embedded JPEG did not draw (Qt JPEG plugin missing?)")


def _check_jxl():
    import imagecodecs
    import numpy as np

    _roundtrip(".jxl", lambda im, p: p.write_bytes(imagecodecs.jpegxl_encode(np.asarray(im))))


def _check_webp():
    _roundtrip(".webp", lambda im, p: im.save(p))


def _check_psd():
    from psd_tools import PSDImage

    def save(im, path):
        psd = PSDImage.new("RGB", im.size, depth=16)  # 16-bit: the case Pillow alone cannot read
        psd.create_pixel_layer(im)
        psd.save(path)

    _roundtrip(".psd", save)


def _check_raw():
    import rawpy

    if not rawpy.libraw_version:
        raise AssertionError("LibRaw not loaded")


def _check_svg():
    from PySide6.QtSvg import QSvgRenderer

    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"/>'
    if not QSvgRenderer(svg).isValid():
        raise AssertionError("Qt SVG renderer failed")


def _check_video_backend():
    from PySide6.QtMultimedia import QMediaFormat

    decodable = QMediaFormat().supportedFileFormats(QMediaFormat.ConversionMode.Decode)
    if not decodable:
        raise AssertionError("Qt multimedia backend reports no decodable containers")


def _check_video_codecs():
    """FFmpeg's stream list, read at fixed structure offsets (videoprobe.py), must still match this FFmpeg."""
    from mediareaderx import content, videoprobe
    from mediareaderx.resources import resource_dir

    probe = resource_dir() / "probe.mp4"
    head = content.read_head(probe)
    size = probe.stat().st_size
    with probe.open("rb") as f:
        found = videoprobe.streams(f, size, content.ffmpeg_demuxer(head, ".mp4", size <= len(head)) or "")
    if found != [("video", "mpeg4"), ("audio", "aac")] or videoprobe.refusal(found) is not None:
        raise AssertionError(f"unexpected streams {found}")
    if videoprobe.refusal([("video", "ffv1")]) is None:
        raise AssertionError("a codec outside the list was allowed")


def _check_sandbox():
    """Protected decoding works: a helper decodes, checks a video, and cannot read files, use the network
    or start programs."""
    from mediareaderx import sandbox
    from mediareaderx.resources import resource_dir

    previous = sandbox.worked_before()
    sandbox.configure(worked_before=True)  # fail closed here: the self-test must not pass in-process
    try:
        _sandbox_checks(sandbox, resource_dir)
    finally:
        sandbox.configure(worked_before=previous)
        sandbox.shutdown()


def _sandbox_checks(sandbox, resource_dir) -> None:
    img = sandbox.decode(resource_dir() / "probe.heic")
    if (img.size.width(), img.size.height()) != (16, 8) or sandbox.state() != "on":
        raise AssertionError(f"protected decode failed (state {sandbox.state()})")
    sandbox.check_video(resource_dir() / "probe.mp4")
    with tempfile.TemporaryDirectory() as tmp:
        secret = Path(tmp) / "secret.txt"
        secret.write_text("not for the helper", encoding="utf-8")
        results = sandbox.selfcheck(secret)
    if results != {"read": "blocked", "network": "blocked", "process": "blocked", "screen": "blocked",
                   "user_desktop": "blocked"}:
        raise AssertionError(f"helper not contained: {results}")


def _check_printing():
    from PySide6.QtGui import QImage, QPainter, QPdfWriter
    from PySide6.QtPrintSupport import QPrinterInfo

    from mediareaderx.printing import Cell, PrintOptions, prepare_device, render_page

    QPrinterInfo.availablePrinterNames()  # loads Qt print support and the Windows printer plugin
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "probe.pdf"
        writer = QPdfWriter(str(path))
        prepare_device(writer, landscape=False)
        painter = QPainter(writer)
        img = QImage(8, 8, QImage.Format.Format_RGB32)
        img.fill(0xC83232)
        render_page(painter, writer.pageLayout().fullRectPixels(writer.resolution()).toRectF(),
                    writer.resolution() / 25.4, [Cell("probe", img)] * 4, PrintOptions(), 4)
        painter.end()
        del writer
        if not path.read_bytes().startswith(b"%PDF"):
            raise AssertionError("PDF writer produced no PDF")


CHECKS = {
    "HEIC (libheif)": _check_heic, "AVIF": _check_avif, "JPEG XL (libjxl)": _check_jxl,
    "WebP": _check_webp, "Photoshop PSD (psd-tools)": _check_psd, "Camera RAW (LibRaw)": _check_raw,
    "SVG (Qt)": _check_svg,
    "Video (Qt FFmpeg backend)": _check_video_backend, "Print and PDF (Qt)": _check_printing,
    "Image decoder lock": _check_image_lock, "Video container check": _check_video_lock,
    "SVG picture check": _check_svg_lock, "Video codec check (FFmpeg streams)": _check_video_codecs,
    "Protected decoding (sandbox)": _check_sandbox,
}


def run_self_test() -> int:
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtGui import QGuiApplication

    # A GUI application object is needed for SVG/QImage, but no window is shown.
    _app = QCoreApplication.instance() or QGuiApplication(["MediaReaderX", "-platform", "offscreen"])
    report = io.StringIO()
    report.write(f"Media Reader x {__version__} self-test\n")
    failures = 0
    for name, check in CHECKS.items():
        try:
            check()
            report.write(f"PASS  {name}\n")
        except Exception as exc:  # report every failure, never stop early
            failures += 1
            report.write(f"FAIL  {name}: {exc!r}\n")
            log.exception("Self-test failed: %s", name)
    report.write("RESULT: " + ("OK" if not failures else f"{failures} FAILED") + "\n")
    text = report.getvalue()
    (data_dir() / "selftest.txt").write_text(text, encoding="utf-8")
    log.info("Self-test:\n%s", text)
    return 1 if failures else 0
