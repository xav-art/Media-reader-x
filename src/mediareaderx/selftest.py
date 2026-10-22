"""Headless health check: ``MediaReaderX.exe --self-test``.

Encodes tiny images in memory with each bundled codec, decodes them back, and
confirms Qt's FFmpeg multimedia backend loads. Results go to the log and to
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


def _check_heic():
    import pillow_heif

    _roundtrip(".heic", lambda im, p: pillow_heif.from_pillow(im).save(p, quality=90))


def _check_avif():
    _roundtrip(".avif", lambda im, p: im.save(p, quality=90))


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
