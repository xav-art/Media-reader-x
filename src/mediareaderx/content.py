"""Decide from a file's first bytes which single decoder may read it.

Pillow and FFmpeg pick a parser from a file's content, not its name. Pillow has 43 openers here (EPS, WMF
through Windows GDI, FITS, PCD...) and Qt's FFmpeg has hundreds of demuxers, including playlists that open
URLs. So the app identifies the content itself, from a fixed list of the families it supports, and then
hands the file to the one decoder for that family. A file whose content does not match its extension
still opens with the decoder for its real content, plus a notice; anything outside the list is refused.

Messages are built only from the fixed labels below, never from bytes in the file.
"""

from __future__ import annotations

import base64
import binascii
import ctypes
import functools
import logging
import zlib
from dataclasses import dataclass
from pathlib import Path

from mediareaderx.formats import IMAGE_FAMILIES, VIDEO_FAMILIES

log = logging.getLogger(__name__)

HEAD_BYTES = 64 * 1024
MAX_SVG_BYTES = 64 * 1024**2  # inflated .svgz cap: real drawings are far smaller


class Refused(Exception):
    """The content is outside what the app opens. ``str(exc)`` is a user-facing message."""


@dataclass(frozen=True)
class Family:
    key: str
    label: str  # "a PNG picture": fixed text, safe to show
    short: str  # "PNG": for the status bar
    ext: str  # the extension this content normally has
    kind: str  # image, video, raw, svg or other


_FAMILIES = [
    Family("jpeg", "a JPEG picture", "JPEG", ".jpg", "image"),
    Family("png", "a PNG picture", "PNG", ".png", "image"),
    Family("gif", "a GIF picture", "GIF", ".gif", "image"),
    Family("bmp", "a BMP picture", "BMP", ".bmp", "image"),
    Family("dib", "a DIB picture", "DIB", ".dib", "image"),
    Family("tiff", "a TIFF picture", "TIFF", ".tif", "image"),
    Family("webp", "a WebP picture", "WebP", ".webp", "image"),
    Family("ico", "an icon", "ICO", ".ico", "image"),
    Family("heic", "a HEIC picture", "HEIC", ".heic", "image"),
    Family("avif", "an AVIF picture", "AVIF", ".avif", "image"),
    Family("jxl", "a JPEG XL picture", "JPEG XL", ".jxl", "image"),
    Family("psd", "a Photoshop file", "PSD", ".psd", "image"),
    Family("qoi", "a QOI picture", "QOI", ".qoi", "image"),
    Family("dds", "a DDS texture", "DDS", ".dds", "image"),
    Family("pnm", "a PNM picture", "PNM", ".ppm", "image"),
    Family("tga", "a TGA picture", "TGA", ".tga", "image"),
    Family("svg", "an SVG drawing", "SVG", ".svg", "svg"),
    Family("cr3", "a Canon CR3 RAW photo", "CR3", ".cr3", "raw"),
    Family("isobmff", "an MP4 or MOV video", "MP4/MOV", ".mp4", "video"),
    Family("matroska", "an MKV or WebM video", "MKV/WebM", ".mkv", "video"),
    Family("avi", "an AVI video", "AVI", ".avi", "video"),
    Family("asf", "a WMV video", "WMV", ".wmv", "video"),
    Family("mpegts", "an MPEG transport stream video", "MPEG-TS", ".ts", "video"),
    Family("mpegps", "an MPEG video", "MPEG", ".mpg", "video"),
    Family("flv", "a Flash video", "FLV", ".flv", "video"),
    Family("ogg", "an Ogg video", "Ogg", ".ogv", "video"),
    Family("exe", "a Windows program", "", "", "other"),
    Family("zip", "a ZIP archive or Office document", "", "", "other"),
    Family("pdf", "a PDF document", "", "", "other"),
    Family("text", "a text file", "", "", "other"),
    Family("jp2", "a JPEG 2000 picture", "", "", "other"),
    # A HEIF picture compressed with AVC (H.264): recognised so the message can say why it does not open.
    Family("heif_avc", "a HEIF picture with AVC compression", "", "", "other"),
]
FAMILIES = {f.key: f for f in _FAMILIES}

# ISO-BMFF brands (ftyp major and compatible brands).
_HEVC_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs"}
_AVIF_BRANDS = {b"avif", b"avis"}
_AVC_BRANDS = {b"avci", b"avcs"}
_GENERIC_HEIF = {b"mif1", b"msf1", b"miaf"}
# First atoms FFmpeg's mov_probe accepts, plus the boxes a fragmented MP4 (DASH segment) starts with
# (mov_probe does not score those; check_video's FFmpeg probe has the final say).
_MOV_ATOMS = {b"ftyp", b"moov", b"mdat", b"pnot", b"udta", b"free", b"skip", b"wide", b"junk", b"pict",
              b"uuid", b"prfl", b"ediw", b"styp", b"moof", b"sidx"}
_ASF_GUID = bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c")
_SVG_STARTS = (b"<?xml", b"<svg", b"<!--", b"<!DOCTYPE svg", b"<!doctype svg")


def read_head(path: str | Path, size: int = HEAD_BYTES) -> bytes:
    with open(path, "rb") as f:
        return f.read(size)


def _ftyp_family(head: bytes) -> str | None:
    """Classify an ISO-BMFF file from its ftyp box: HEIC, AVIF, CR3 or video."""
    size = int.from_bytes(head[0:4], "big")
    if head[4:8] != b"ftyp" or size < 16:
        return None
    major = head[8:12]
    brands = {major} | {head[i:i + 4] for i in range(16, min(size, len(head)) - 3, 4)}
    if major == b"crx ":
        return "cr3"
    if major in (b"jp2 ", b"jpx ", b"jpm "):
        return "jp2"  # JPEG 2000 still image: not supported, and FFmpeg's mov probe scores it low
    if brands & _AVIF_BRANDS:
        return "avif"
    if brands & _HEVC_BRANDS:
        return "heic"
    if brands & _AVC_BRANDS:
        return "heif_avc"
    if major in _GENERIC_HEIF:
        return "heic"  # HEIF with no codec brand: libheif reports the codec if it cannot decode it
    return "isobmff"


def _mov_atoms(head: bytes) -> bool:
    """Walk the top-level atoms like FFmpeg's mov_probe: one atom it knows, reached through sane sizes."""
    pos = 0
    while pos + 8 <= len(head):
        size, tag = int.from_bytes(head[pos:pos + 4], "big"), head[pos + 4:pos + 8]
        if not all(0x20 <= c < 0x7F for c in tag):
            return False
        if tag in _MOV_ATOMS:
            return True
        if size == 1 and pos + 16 <= len(head):
            size = int.from_bytes(head[pos + 8:pos + 16], "big")
        if size < 8:
            return False
        pos += size
    return False


def _ts_sync(head: bytes) -> bool:
    """MPEG-TS from the first byte: sync 0x47 every 188 or 204 bytes, or at +4 every 192 (M2TS, AVCHD)."""
    for start, stride in ((0, 188), (0, 204), (4, 192)):
        if len(head) >= start + stride * 4 + 1 and all(head[start + k * stride] == 0x47 for k in range(5)):
            return True
    return False


def _mpeg_ps(head: bytes) -> bool:
    """MPEG program stream or MPEG-1/2 video: a pack or sequence header first, after optional zero padding."""
    rest = head.lstrip(b"\x00")
    return len(head) - len(rest) >= 2 and rest[:2] in (b"\x01\xba", b"\x01\xb3")


def _is_text(head: bytes) -> bool:
    sample = head[:64]
    return len(sample) >= 4 and all(c in (9, 10, 13) or 0x20 <= c < 0x7F for c in sample)


def sniff(head: bytes) -> Family | None:
    """The family of ``head`` (the first bytes of a file), or None when it is none the app knows."""
    h = head
    key: str | None = None
    if h[:3] == b"\xff\xd8\xff":
        key = "jpeg"
    elif h[:8] == b"\x89PNG\r\n\x1a\n":
        key = "png"
    elif h[:6] in (b"GIF87a", b"GIF89a"):
        key = "gif"
    elif h[:2] == b"BM" and len(h) >= 18 and int.from_bytes(h[14:18], "little") in (12, 40, 52, 56, 64, 108, 124):
        key = "bmp"
    elif h[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
        key = "tiff"
    elif h[:4] == b"RIFF" and h[8:12] == b"WEBP":
        key = "webp"
    elif h[:4] == b"RIFF" and h[8:12] == b"AVI ":
        key = "avi"
    elif h[:4] == b"\x00\x00\x01\x00" and len(h) >= 6 and int.from_bytes(h[4:6], "little") > 0:
        key = "ico"
    elif h[4:8] == b"ftyp":
        key = _ftyp_family(h) or "isobmff"
    elif h[:2] == b"\xff\x0a" or h[:12] == b"\x00\x00\x00\x0cJXL \x0d\x0a\x87\x0a":
        key = "jxl"
    elif h[:4] == b"8BPS" and h[4:6] in (b"\x00\x01", b"\x00\x02"):
        key = "psd"
    elif h[:4] == b"qoif":
        key = "qoi"
    elif h[:4] == b"DDS ":
        key = "dds"
    elif h[:1] == b"P" and len(h) >= 3 and h[1] in b"123456fF" and h[2] in b" \t\r\n":
        key = "pnm"
    elif h[:4] == b"\x1a\x45\xdf\xa3":
        key = "matroska"
    elif h[:16] == _ASF_GUID:
        key = "asf"
    elif h[:4] == b"FLV\x01":
        key = "flv"
    elif h[:4] == b"OggS":
        key = "ogg"
    elif _mpeg_ps(h):
        key = "mpegps"
    elif _ts_sync(h):
        key = "mpegts"
    elif _mov_atoms(h):
        key = "isobmff"
    elif h[:2] == b"MZ":
        key = "exe"
    elif h[:4] == b"PK\x03\x04":
        key = "zip"
    elif h[:5] == b"%PDF-":
        key = "pdf"
    elif h[:2] == b"\x1f\x8b":
        key = None  # gzip: only .svgz is gzip here, and the SVG loader inflates it itself
    else:
        stripped = h.lstrip(b"\xef\xbb\xbf \t\r\n")
        if stripped.startswith(_SVG_STARTS) and b"<svg" in h[:4096]:
            key = "svg"
        elif _is_text(h):
            key = "text"
    return FAMILIES[key] if key else None


def mismatch_notice(found: Family, ext: str) -> str:
    """Status-bar text for a file that opened with the decoder for its real content."""
    return f"{found.short} file named {ext}"


def refusal(found: Family | None, ext: str, wanted: str) -> str:
    """The message for content that the app will not open under ``ext`` (``wanted``: image or video)."""
    if found is None:
        return f"The file is damaged or is not a supported {wanted}."
    wanted = "picture" if wanted == "image" else wanted
    if found.key == "heif_avc":
        return "This HEIF picture uses AVC (H.264) compression. Media Reader x cannot open it."
    if found.kind == "other":
        return (f"This file has the extension {ext}, but it contains {found.label}, not a {wanted}. "
                "Media Reader x does not open it.")
    return (f"This file has the extension {ext}, but it contains {found.label}. "
            f"To open it, change the extension to {found.ext}.")


@dataclass(frozen=True)
class Route:
    family: str
    notice: str | None = None


def route_image(ext: str, head: bytes) -> Route:
    """The one image family to decode ``head`` with, for a file named ``*{ext}``. Raises Refused."""
    ext = ext.lower()
    expected = IMAGE_FAMILIES[ext]
    found = sniff(head)
    if found is None:
        # TGA and headerless DIB have no signature: only their own parser, chosen by the extension, may try.
        for key in expected:
            if key in ("tga", "dib"):
                return Route(key)
        raise Refused(refusal(None, ext, "image"))
    if found.key in expected:
        return Route(found.key)
    if found.kind == "image":
        return Route(found.key, mismatch_notice(found, ext))
    raise Refused(refusal(found, ext, "image"))


def check_video(path: str | Path) -> str | None:
    """Return a notice when the container differs from the extension's. Raises Refused for non-video.

    Two checks must agree: the file starts with a known container signature, and FFmpeg's own probe
    picks the demuxer for that same container. So no playlist (HLS, concat, SDP), image-pipe or other
    demuxer is ever chosen. URLs are also blocked by the protocol whitelist set in ``__init__``.
    The check does not choose the codec inside the container.
    """
    path = Path(path)
    try:
        head = read_head(path)
        size = path.stat().st_size
    except OSError:
        # Refuse, never "let the player report it": the player would open the file without this check.
        raise Refused("The file cannot be read. It may have been moved or you may lack access.") from None
    return check_video_head(head, path.suffix, size)


def check_video_head(head: bytes, ext: str, size: int) -> str | None:
    """``check_video`` for the first bytes of a file named ``*{ext}`` that is ``size`` bytes long."""
    ext = ext.lower()
    found = sniff(head)
    if found is None or found.kind != "video":
        raise Refused(refusal(found, ext, "video"))
    try:
        demuxer = ffmpeg_demuxer(head, ext, at_end=size <= len(head))
    except (OSError, AttributeError, ctypes.ArgumentError):  # a broken bundle: refuse, never crash the GUI
        log.exception("FFmpeg probe unavailable")
        raise Refused("Media Reader x cannot check this video. To see why, select Help > Open log folder.") from None
    if demuxer not in _DEMUXERS[found.key]:
        log.warning("Video refused: content %s, FFmpeg would use another demuxer", found.key)
        raise Refused(refusal(None, ext, "video"))
    if found.key in VIDEO_FAMILIES.get(ext, ()):
        return None
    return mismatch_notice(found, ext)


# The FFmpeg demuxers each video family may get (AVInputFormat.name in FFmpeg 9).
_DEMUXERS = {
    "isobmff": ("mov,mp4,m4a,3gp,3g2,mj2",), "matroska": ("matroska,webm",), "avi": ("avi",), "asf": ("asf",),
    "mpegts": ("mpegts",), "mpegps": ("mpeg", "mpegvideo"),  # mpegvideo: a bare MPEG-1/2 stream named .mpg
    "flv": ("flv",), "ogg": ("ogg",),
}
_PROBE_MIN = 2048  # FFmpeg's PROBE_BUF_MIN
_SCORE_RETRY = 25  # FFmpeg's AVPROBE_SCORE_RETRY: below this it reads more data and probes again
_PROBE_PADDING = 64  # FFmpeg needs AVPROBE_PADDING_SIZE (32) zero bytes after the data


class _ProbeData(ctypes.Structure):
    _fields_ = [("filename", ctypes.c_char_p), ("buf", ctypes.POINTER(ctypes.c_ubyte)),
                ("buf_size", ctypes.c_int), ("mime_type", ctypes.c_char_p)]


@functools.cache
def _probe_function():
    """``av_probe_input_format3`` from the avformat DLL that Qt Multimedia ships. Raises OSError."""
    import PySide6

    dlls = sorted(Path(PySide6.__file__).parent.glob("avformat-*.dll"))
    if len(dlls) != 1:
        raise OSError(f"expected one avformat DLL, found {len(dlls)}")
    fn = ctypes.CDLL(str(dlls[0])).av_probe_input_format3
    fn.restype = ctypes.c_void_p
    fn.argtypes = [ctypes.POINTER(_ProbeData), ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    return fn


def ffmpeg_demuxer(head: bytes, ext: str, at_end: bool) -> str | None:
    """The demuxer FFmpeg would pick for a file that starts with ``head``, or None if it would read on.

    Follows FFmpeg's av_probe_input_buffer2: probe 2 KB, 4 KB... and stop at the first score above
    AVPROBE_SCORE_RETRY. If no format is that sure within ``head``, FFmpeg would read further than this
    check can see, so the answer is None (refused), unless ``head`` is the whole file (``at_end``).
    """
    fn = _probe_function()
    size = min(_PROBE_MIN, len(head))
    while True:
        last = size >= len(head)
        chunk = head[:size]
        buf = (ctypes.c_ubyte * (len(chunk) + _PROBE_PADDING)).from_buffer_copy(chunk + bytes(_PROBE_PADDING))
        data = _ProbeData(f"probe{ext}".encode("ascii", "replace"), buf, len(chunk), None)
        score = ctypes.c_int(0)
        fmt = fn(ctypes.byref(data), 1, ctypes.byref(score))
        if fmt and (score.value > _SCORE_RETRY or (last and at_end and score.value > 0)):
            return ctypes.c_char_p.from_address(fmt).value.decode("ascii", "replace")
        if last:
            return None
        size = min(size * 2, len(head))


# -- SVG ---------------------------------------------------------------------------------------------
def _check_data_uri(value: str) -> None:
    header, sep, payload = value.partition(",")
    if not sep or not header.lower().endswith(";base64"):
        raise Refused("This SVG file contains a picture in a form Media Reader x does not open.")
    try:
        data = base64.b64decode("".join(payload.split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise Refused("The SVG file is damaged.") from exc
    found = sniff(data[:HEAD_BYTES])
    if found is None or found.key not in ("png", "jpeg"):
        raise Refused("This SVG file contains a picture that is not PNG or JPEG. Media Reader x does not open it.")


def load_svg(path: str | Path) -> bytes:
    """``check_svg`` for a file. Raises Refused."""
    return check_svg(Path(path).read_bytes())


def check_svg(raw: bytes) -> bytes:
    """Check an SVG or SVGZ file's content and return plain SVG bytes that are safe to give to Qt's renderer.

    Qt's SVG renderer loads any picture an <image> names: files anywhere on the PC, and embedded data
    in any format Qt has a plugin for. Only embedded PNG and JPEG pictures are allowed, checked by
    content; links to files or the web, other embedded types and DTD entity declarations are refused.
    Raises Refused.
    """
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QXmlStreamReader

    if raw[:2] == b"\x1f\x8b":
        inflater = zlib.decompressobj(wbits=31)
        try:
            data = inflater.decompress(raw, MAX_SVG_BYTES)
        except zlib.error as exc:
            raise Refused("The SVG file is damaged.") from exc
        if inflater.unconsumed_tail:
            raise Refused("The SVG file is too large to open safely.")
    else:
        data = raw
    # From bytes, like Qt's renderer, so both detect the same text encoding.
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QXmlStreamReader(buffer)
    reader.setNamespaceProcessing(False)  # like Qt's SVG handler: names stay as written ("xlink:href")
    seen_root = False
    while not reader.atEnd():
        token = reader.readNext()
        if token == QXmlStreamReader.TokenType.DTD:
            if reader.entityDeclarations() or "[" in reader.text():
                raise Refused("This SVG file declares its own entities. Media Reader x does not open it.")
        elif token == QXmlStreamReader.TokenType.StartElement:
            element = reader.name().rpartition(":")[2]
            if not seen_root and element != "svg":
                raise Refused("The file is damaged or is not an SVG drawing.")
            seen_root = True
            for attr in reader.attributes():
                if attr.name().rpartition(":")[2] != "href":
                    continue
                value = attr.value()  # exactly as Qt reads it: no trimming, case-sensitive
                if element == "image":
                    # Qt loads any <image> href that does not start with "data" as a file path, even "#x.png".
                    if not value.startswith("data:"):
                        raise Refused("This SVG file links to another file or a web address. "
                                      "Media Reader x does not open it.")
                    _check_data_uri(value)
                elif element == "feImage" and value.startswith("data:"):
                    _check_data_uri(value)
                elif not value.startswith("#"):  # elsewhere only references inside this document
                    raise Refused("This SVG file links to another file or a web address. "
                                  "Media Reader x does not open it.")
    if reader.hasError() or not seen_root:
        raise Refused("The file is damaged or is not an SVG drawing.")
    return data
