"""Regression: EXIF sub-IFDs (Exif, GPS) are read lazily from the file.

1.1.0 serialised EXIF after the file closed, so RAW files and JPEGs with GPS
failed to open ("seek of closed file"). These tests use file-backed EXIF.
"""

import os
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from mediareaderx.decoders import decode
from mediareaderx.export import export_image
from mediareaderx.metadata import EXIF_IFD, GPS_IFD


def _exif_with_subifds() -> Image.Exif:
    exif = Image.Exif()
    exif[0x010F] = "NIKON CORPORATION"
    exif[0x0110] = "NIKON D3S"
    exif.get_ifd(EXIF_IFD)[0x9003] = "2012:03:04 17:20:59"  # DateTimeOriginal
    exif.get_ifd(GPS_IFD).update({1: "N", 2: (Fraction(55), Fraction(40), Fraction(0)),
                                  3: "E", 4: (Fraction(12), Fraction(34), Fraction(0))})
    return exif


@pytest.mark.parametrize("ext, fmt", [(".jpg", "JPEG"), (".tif", "TIFF")])
def test_file_backed_exif_with_gps_opens_and_saves(tmp_path, qapp, ext, fmt):
    src = tmp_path / f"photo{ext}"
    Image.new("RGB", (40, 30), "navy").save(src, fmt, exif=_exif_with_subifds().tobytes())
    img = decode(src)
    rows = {(m.section, m.key): m.value for m in img.metadata}
    assert rows[("Camera", "Date taken")] == "2012:03:04 17:20:59"
    assert ("Location (GPS)", "Coordinates") in rows
    assert img.exif_bytes
    out = export_image(img.frames[0], tmp_path / "copy.jpg", source=src, exif_bytes=img.exif_bytes)
    with Image.open(out) as saved:
        assert saved.getexif()[0x0110] == "NIKON D3S"


def test_raw_path_reads_exif_before_closing(tmp_path, qapp, monkeypatch):
    """Exercise _decode_raw with LibRaw stubbed and a TIFF-based '.nef' for the EXIF part."""
    import rawpy

    src = tmp_path / "camera.nef"
    Image.new("RGB", (40, 30), "navy").save(src, "TIFF", exif=_exif_with_subifds().tobytes())

    class FakeRaw:
        sizes = SimpleNamespace(raw_width=44, raw_height=32, width=40, height=30)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def postprocess(self, **_kw):
            return np.zeros((30, 40, 3), dtype=np.uint8)

    monkeypatch.setattr(rawpy, "imread", lambda _p: FakeRaw())
    img = decode(src)
    rows = {(m.section, m.key): m.value for m in img.metadata}
    assert rows[("Camera", "Camera model")] == "NIKON D3S"
    assert rows[("Camera", "Date taken")] == "2012:03:04 17:20:59"
    assert img.exif_bytes


@pytest.mark.skipif(not os.environ.get("MEDIAREADER_RAW_SAMPLE"), reason="set MEDIAREADER_RAW_SAMPLE to a real RAW file")
def test_real_raw_sample(qapp):
    img = decode(Path(os.environ["MEDIAREADER_RAW_SAMPLE"]))
    assert img.format_name.startswith("Camera RAW") and img.exif_bytes


def test_jpg_copy_drops_huge_maker_note_but_keeps_camera_info(tmp_path, qapp):
    """Regression: RAW EXIF with a large MakerNote exceeded the 64 KB JPEG limit."""
    exif = _exif_with_subifds()
    exif.get_ifd(EXIF_IFD)[0x927C] = b"\x00" * 90_000  # MakerNote
    src = tmp_path / "big.tif"
    Image.new("RGB", (40, 30), "navy").save(src, "TIFF", exif=exif.tobytes())
    img = decode(src)
    out = export_image(img.frames[0], tmp_path / "copy.jpg", source=src, exif_bytes=img.exif_bytes)
    with Image.open(out) as saved:
        assert saved.getexif()[0x0110] == "NIKON D3S"
        assert 0x927C not in saved.getexif().get_ifd(EXIF_IFD)


def test_jpg_copy_without_exif_when_still_too_large(tmp_path, qapp):
    exif = _exif_with_subifds()
    exif[0x010E] = "x" * 80_000  # ImageDescription: not removable like a MakerNote
    img = decode_png_with_exif(tmp_path, exif)
    out = export_image(img.frames[0], tmp_path / "copy.jpg", exif_bytes=exif.tobytes())
    with Image.open(out) as saved:
        assert saved.size == (40, 30) and 0x010E not in saved.getexif()


def decode_png_with_exif(tmp_path, exif):
    src = tmp_path / "plain.png"
    Image.new("RGB", (40, 30), "navy").save(src)
    return decode(src)


@pytest.mark.skipif(not os.environ.get("MEDIAREADER_RAW_SAMPLE"), reason="set MEDIAREADER_RAW_SAMPLE to a real RAW file")
def test_real_raw_saves_as_jpg(tmp_path, qapp):
    src = Path(os.environ["MEDIAREADER_RAW_SAMPLE"])
    img = decode(src)
    out = export_image(img.frames[0], tmp_path / "copy.jpg", source=src, exif_bytes=img.exif_bytes)
    with Image.open(out) as saved:
        assert saved.getexif().get(0x010F)  # camera make kept
