"""Turn file, EXIF and GPS information into rows for the metadata panel."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from PIL import ExifTags, Image


class MetaItem(NamedTuple):
    section: str
    key: str
    value: str


EXIF_IFD = 0x8769
GPS_IFD = 0x8825
# Large binary or vendor blobs that are noise in a viewer.
_SKIP_TAGS = {"MakerNote", "PrintImageMatching", "JPEGInterchangeFormat",
              "JPEGInterchangeFormatLength", "ExifOffset", "GPSInfo", "StripOffsets",
              "StripByteCounts", "TileOffsets", "TileByteCounts", "XMLPacket"}
_SUMMARY_TAGS = [
    ("Make", "Camera make"), ("Model", "Camera model"), ("LensModel", "Lens"),
    ("DateTimeOriginal", "Date taken"), ("ExposureTime", "Exposure"),
    ("FNumber", "Aperture"), ("ISOSpeedRatings", "ISO"), ("FocalLength", "Focal length"),
    ("FocalLengthIn35mmFilm", "Focal length (35 mm)"), ("Software", "Software"),
    ("Artist", "Artist"), ("Copyright", "Copyright"),
]


def human_size(num: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "bytes" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


def file_items(path: str | Path) -> list[MetaItem]:
    p = Path(path)
    st = p.stat()
    return [
        MetaItem("File", "Name", p.name),
        MetaItem("File", "Folder", str(p.parent)),
        MetaItem("File", "Size", f"{human_size(st.st_size)} ({st.st_size:,} bytes)"),
        MetaItem("File", "Modified", datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")),
    ]


def _fmt(value) -> str:
    if isinstance(value, bytes):
        text = value.rstrip(b"\x00")
        if len(text) <= 64 and all(32 <= b < 127 for b in text):
            return text.decode("ascii")
        return f"<{len(value)} bytes>"
    if isinstance(value, tuple):
        return ", ".join(_fmt(v) for v in value)
    if isinstance(value, float) or hasattr(value, "numerator"):
        f = float(value)
        return f"{f:.4g}"
    return str(value).strip("\x00 ")


def _summary_value(name: str, value) -> str:
    try:
        if name == "ExposureTime":
            f = float(value)
            return f"1/{round(1 / f)} s" if 0 < f < 1 else f"{f:g} s"
        if name == "FNumber":
            return f"f/{float(value):.1f}"
        if name.startswith("FocalLength"):
            return f"{float(value):g} mm"
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    return _fmt(value)


def _dms_to_decimal(dms, ref) -> float | None:
    try:
        d, m, s = (float(x) for x in dms)
    except (TypeError, ValueError):
        return None
    dec = d + m / 60 + s / 3600
    ref = ref.decode() if isinstance(ref, bytes) else str(ref or "")
    return -dec if ref.upper().startswith(("S", "W")) else dec


def gps_items(gps: dict) -> list[MetaItem]:
    """Decode a GPS IFD (tag id -> value) into readable rows."""
    named = {ExifTags.GPSTAGS.get(k, str(k)): v for k, v in gps.items()}
    rows: list[MetaItem] = []
    lat = _dms_to_decimal(named.get("GPSLatitude"), named.get("GPSLatitudeRef"))
    lon = _dms_to_decimal(named.get("GPSLongitude"), named.get("GPSLongitudeRef"))
    if lat is not None and lon is not None:
        rows.append(MetaItem("Location (GPS)", "Coordinates", f"{lat:.6f}, {lon:.6f}"))
    if "GPSAltitude" in named:
        try:
            alt = float(named["GPSAltitude"])
            below = named.get("GPSAltitudeRef") in (1, b"\x01")
            rows.append(MetaItem("Location (GPS)", "Altitude", f"{-alt if below else alt:.1f} m"))
        except (TypeError, ValueError):
            pass
    for key in ("GPSDateStamp", "GPSImgDirection", "GPSSpeed"):
        if key in named:
            rows.append(MetaItem("Location (GPS)", key.removeprefix("GPS"), _fmt(named[key])))
    return rows


def exif_items(exif: Image.Exif | None) -> list[MetaItem]:
    if not exif:
        return []
    tags: dict[str, object] = {}
    for tag_id, value in exif.items():
        tags[ExifTags.TAGS.get(tag_id, f"Tag 0x{tag_id:04X}")] = value
    try:
        for tag_id, value in exif.get_ifd(EXIF_IFD).items():
            tags[ExifTags.TAGS.get(tag_id, f"Tag 0x{tag_id:04X}")] = value
    except (KeyError, ValueError, TypeError):
        pass

    rows = [MetaItem("Camera", label, _summary_value(name, tags[name]))
            for name, label in _SUMMARY_TAGS if name in tags and _fmt(tags[name])]
    try:
        rows += gps_items(exif.get_ifd(GPS_IFD))
    except (KeyError, ValueError, TypeError):
        pass
    rows += [MetaItem("EXIF (all tags)", name, _fmt(value))
             for name, value in sorted(tags.items()) if name not in _SKIP_TAGS]
    return rows
