"""Image decoding for every still/animated format Media Reader x supports.

All decoders return a :class:`DecodedImage` holding Qt ``QImage`` frames that
are already colour-managed to sRGB and rotated per EXIF orientation. Decoding
is pure (no widgets), so it runs safely on a worker thread.
"""

from __future__ import annotations

import functools
import gc
import io
import logging
import os
import struct
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError
from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

from mediareaderx import config, content
from mediareaderx.decoded import DecodedImage, DecodeError, check_file_size, image_kind  # noqa: F401
from mediareaderx.formats import MediaKind, kind_of
from mediareaderx.metadata import MetaItem, exif_items, file_items

log = logging.getLogger(__name__)

_registered = False


def _fit_side(width: int, height: int, max_side: int | None) -> tuple[int, int] | None:
    """The size that fits ``max_side`` on the long side, or None when no reduction is needed."""
    if not max_side or max(width, height) <= max_side:
        return None
    scale = max_side / max(width, height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def _shrink_pil(im: Image.Image, max_side: int | None) -> Image.Image:
    """Reduce a still to ``max_side`` before colour conversion: every later step is then cheaper.

    A JPEG decodes straight at 1/2, 1/4 or 1/8 scale (DCT scaling), so a 48 MP photo never
    exists at full size. Other formats decode in full on the worker, then shrink in place.
    """
    target = _fit_side(*im.size, max_side)
    if target is None:
        return im
    if im.format in JPEG_FORMATS:
        im.draft("RGB", target)  # picks the smallest DCT scale that is still >= target
    if im.mode in ("I;16", "I;16B", "I;16L", "I", "F"):
        info, im = im.info, _normalise_mode(im)
        im.info = info  # keep the ICC profile for _to_srgb
    # A whole-number box reduce first, then one short resample: 48 MP 362 -> 87 ms (review, measured).
    # BICUBIC, not LANCZOS: the copy is drawn through Qt's bilinear filter, so the difference never shows.
    factor = max(im.size) // max_side
    if factor >= 2:
        im = im.reduce(factor)  # a new, smaller image; the full one is freed with the caller's reference
    if im.size != target:  # resize, not thumbnail: thumbnail re-rounds the shape and can lose a pixel
        im = im.resize(target, Image.Resampling.BICUBIC)
    return im


# The one Pillow plugin each content family may reach (content.py decides the family). Pillow tries
# only the plugins named in ``formats=``, so its other openers (EPS, WMF through GDI, FITS...) stay unused.
PILLOW_PLUGINS = {
    "jpeg": "JPEG", "png": "PNG", "gif": "GIF", "bmp": "BMP", "dib": "DIB", "tiff": "TIFF", "webp": "WEBP",
    "ico": "ICO", "heic": "HEIF", "avif": "AVIF", "qoi": "QOI", "dds": "DDS", "pnm": "PPM", "tga": "TGA",
}


def register_plugins() -> None:
    """Register optional Pillow plugins once (HEIF via libheif)."""
    global _registered
    if not _registered:
        import pillow_heif

        pillow_heif.options.DISABLE_SECURITY_LIMITS = False  # libheif's own limits (the default), never relaxed
        # AVIF is decoded by Pillow's native plugin; libheif only handles HEIC/HEIF.
        pillow_heif.register_heif_opener(thumbnails=False)
        Image.MAX_IMAGE_PIXELS = config.MAX_IMAGE_PIXELS
        _registered = True
    # Viewing only: unregister the HEIF writer, so nothing in the app can reach the bundled x265 encoder.
    # On every call (two dict lookups), so a later registration elsewhere cannot bring it back.
    Image.SAVE.pop("HEIF", None)
    Image.SAVE_ALL.pop("HEIF", None)


def open_pillow(data: bytes, family: str) -> Image.Image:
    """Open ``data`` with the single Pillow plugin for ``family``. Raises DecodeError if it does not match."""
    register_plugins()
    try:
        return Image.open(io.BytesIO(data), formats=(PILLOW_PLUGINS[family],))
    except UnidentifiedImageError as exc:
        raise DecodeError("The file is damaged or is not a supported image.") from exc
    except Image.DecompressionBombError as exc:
        raise DecodeError("The image is too large to open safely.") from exc


def route_ext(ext: str, head: bytes) -> content.Route:
    """The content family to decode a file named ``*{ext}`` with. Raises DecodeError for content the app
    does not open."""
    try:
        return content.route_image(ext, head)
    except content.Refused as exc:
        raise DecodeError(str(exc)) from exc


def route(path: Path, head: bytes) -> content.Route:
    return route_ext(path.suffix, head)


def warm_up() -> None:
    """Load codec libraries and plugins ahead of the first real decode (about 200 ms)."""
    try:
        register_plugins()
        import imagecodecs  # noqa: F401  (JPEG XL)
        import rawpy  # noqa: F401  (LibRaw)

        from mediareaderx.resources import resource_dir

        with open_pillow((resource_dir() / "probe.avif").read_bytes(), "avif") as im:
            im.load()
    except Exception:  # warming is an optimisation only
        log.debug("Codec warm-up skipped", exc_info=True)


@functools.lru_cache(maxsize=16)
def _srgb_transform(icc: bytes, in_mode: str, out_mode: str):
    """Build each profile-to-sRGB transform once: building it costs more than applying it."""
    src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
    return ImageCms.buildTransform(src, ImageCms.createProfile("sRGB"), in_mode, out_mode)


@functools.lru_cache(maxsize=16)
def _is_srgb(icc: bytes) -> bool:
    """Most camera and phone JPEGs embed an sRGB profile: converting sRGB to sRGB costs 50 ms
    per screen copy and 530 ms at 48 MP for no change (review, measured)."""
    try:
        desc = ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc)))
    except (ImageCms.PyCMSError, OSError, ValueError):
        return False
    return desc.strip().lower().startswith("srgb")


def _to_srgb(im: Image.Image) -> Image.Image:
    """Convert embedded ICC profiles (e.g. iPhone Display P3) to sRGB."""
    icc = im.info.get("icc_profile")
    if not icc or im.mode not in ("RGB", "RGBA", "CMYK") or (im.mode != "CMYK" and _is_srgb(icc)):
        return im
    try:
        out_mode = "RGBA" if im.mode == "RGBA" else "RGB"
        return ImageCms.applyTransform(im, _srgb_transform(icc, im.mode, out_mode))
    except (ImageCms.PyCMSError, OSError, ValueError):
        log.debug("ICC conversion skipped", exc_info=True)
        return im


def check_pixels(width: int, height: int) -> None:
    """Refuse decompression bombs ourselves: Pillow only raises above twice its limit."""
    if width * height > config.MAX_IMAGE_PIXELS:
        raise DecodeError(f"The image is too large to open safely ({width} x {height} pixels).")


JXL_HEADER_BYTES = 16  # signature (2 bytes) + SizeHeader (at most 14 bytes)


def jxl_size(data: bytes) -> tuple[int, int] | None:
    """Read width and height from a JPEG XL header without decoding (spec SizeHeader). None if unreadable."""
    if data[:2] == b"\xff\x0a":
        code = data
    elif data[:12] == b"\x00\x00\x00\x0cJXL \x0d\x0a\x87\x0a":
        # ISO-BMFF boxes: the codestream is one jxlc box, or split over jxlp boxes that libjxl joins in order.
        # Join the jxlp parts until the size header is complete: a first part of two bytes must not hide it.
        code, pos = b"", 12
        while pos + 8 <= len(data) and len(code) < JXL_HEADER_BYTES:
            size, kind = int.from_bytes(data[pos:pos + 4], "big"), data[pos + 4:pos + 8]
            header = 8
            if size == 1:
                size, header = int.from_bytes(data[pos + 8:pos + 16], "big"), 16
            elif size == 0:
                size = len(data) - pos
            if size < header:
                return None
            if kind == b"jxlc":
                code += data[pos + header:pos + size]
                break
            if kind == b"jxlp":
                code += data[pos + header + 4:pos + size]  # skip the 4-byte part index
            pos += size
    else:
        return None
    if len(code) < JXL_HEADER_BYTES or code[:2] != b"\xff\x0a":
        return None  # too short to hold the size header: the caller refuses the file
    bits = int.from_bytes(code[2:16], "little")
    pos = 0

    def u(n: int) -> int:
        nonlocal pos
        value = (bits >> pos) & ((1 << n) - 1)
        pos += n
        return value

    def u32() -> int:
        return 1 + u((9, 13, 18, 30)[u(2)])

    div8 = u(1)
    height = (1 + u(5)) * 8 if div8 else u32()
    ratio = u(3)
    if ratio == 0:
        width = (1 + u(5)) * 8 if div8 else u32()
    else:
        num, den = ((1, 1), (12, 10), (4, 3), (3, 2), (16, 9), (5, 4), (2, 1))[ratio - 1]
        width = height * num // den
    return width, height


def _normalise_mode(im: Image.Image) -> Image.Image:
    if im.mode in ("I;16", "I;16B", "I;16L", "I", "F"):
        arr = np.asarray(im, dtype=np.float64)
        hi = float(arr.max()) or 1.0
        return Image.fromarray((arr / hi * 255).clip(0, 255).astype(np.uint8), "L").convert("RGB")
    if im.mode in ("RGBA", "RGB"):
        return im
    has_alpha = im.mode in ("LA", "PA", "La") or (im.mode == "P" and "transparency" in im.info)
    return im.convert("RGBA" if has_alpha else "RGB")


def pil_to_qimage(im: Image.Image) -> QImage:
    im = _normalise_mode(im)
    # Produce Qt's native screen formats (QPixmap.fromImage then needs no conversion, about
    # 10 ms -> 0.3 ms per 12 MP photo shown). Pillow's packed bytes wrapped as-is, then one Qt
    # conversion, which also detaches from the Python buffer: a 48 MP photo peaks 139 MB lower
    # and converts in 129 ms instead of 164 ms (review, measured).
    if im.mode == "RGBA":
        data = im.tobytes("raw", "RGBA")
        src = QImage(data, im.width, im.height, im.width * 4, QImage.Format.Format_RGBA8888)
        return src.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    data = im.tobytes("raw", "RGB")
    src = QImage(data, im.width, im.height, im.width * 3, QImage.Format.Format_RGB888)
    return src.convertToFormat(QImage.Format.Format_RGB32)


def ndarray_to_qimage(arr: np.ndarray, max_side: int | None = None) -> QImage:
    if arr.dtype != np.uint8:
        hi = float(arr.max()) or 1.0
        arr = (arr.astype(np.float64) / hi * 255).clip(0, 255).astype(np.uint8)
    if arr.ndim == 2:
        arr = np.stack([arr] * 3, axis=-1)
    if arr.shape[-1] == 2:  # grey + alpha
        arr = np.concatenate([arr[..., :1]] * 3 + [arr[..., 1:]], axis=-1)
    mode = "RGBA" if arr.shape[-1] == 4 else "RGB"
    return pil_to_qimage(_shrink_pil(Image.fromarray(np.ascontiguousarray(arr), mode), max_side))


def _image_items(fmt: str, size: QSize, frames: int, extra: list[MetaItem] | None = None) -> list[MetaItem]:
    rows = [MetaItem("Image", "Format", fmt),
            MetaItem("Image", "Dimensions", f"{size.width()} x {size.height()} px"),
            MetaItem("Image", "Megapixels", f"{size.width() * size.height() / 1e6:.1f} MP")]
    if frames > 1:
        rows.append(MetaItem("Image", "Frames", str(frames)))
    return rows + (extra or [])


ORIENTATION_TAG = 0x0112
# Pillow names a multi-picture JPEG (MPO) by its own format; it decodes like a JPEG (DCT scaling).
JPEG_FORMATS = frozenset({"JPEG", "MPO"})
# libjxl decodes on one thread by default: 4 threads make a 24 MP file 3.4x faster (review, measured).
JXL_THREADS = min(4, os.cpu_count() or 1)
# EXIF orientation -> the transpose that makes the picture upright (as ImageOps.exif_transpose).
EXIF_TRANSPOSE = {2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180, 4: Image.Transpose.FLIP_TOP_BOTTOM,
              5: Image.Transpose.TRANSPOSE, 6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
              8: Image.Transpose.ROTATE_90}


def _decode_pillow(data: bytes, family: str, max_side: int | None = None) -> DecodedImage:
    # The caller read the file into memory, so it is not held open (and locked) while decoding.
    with open_pillow(data, family) as im:
        check_pixels(*im.size)
        fmt = "JPEG (MPO)" if im.format == "MPO" else im.format or family.upper()
        exif = im.getexif()
        icc_rows = []
        if im.info.get("icc_profile"):
            try:
                desc = ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(im.info["icc_profile"])))
                icc_rows.append(MetaItem("Image", "Colour profile", desc.strip()))
            except (ImageCms.PyCMSError, OSError):
                pass
        # An MPO (iPhone HDR JPEG, some Samsung, Fuji and Nikon photos) holds extra pictures, such as a gain map,
        # that Pillow reports as animation frames. Show the primary picture as a still photo.
        animated = getattr(im, "is_animated", False) and im.format != "MPO"
        n_frames = getattr(im, "n_frames", 1) if animated else 1
        frames: list[QImage] = []
        durations: list[int] = []
        budget = config.MAX_ANIMATION_BYTES
        full_size = None
        for i in range(min(n_frames, config.MAX_ANIMATION_FRAMES)):
            if i:
                im.seek(i)
                check_pixels(*im.size)  # each page or frame has its own size (multi-page TIFF)
            # Stills shrink to the screen and honour EXIF orientation, both in place (no extra
            # full-size copy); animation frames are composited copies at full size.
            if n_frames == 1:
                orientation = exif.get(ORIENTATION_TAG, 1)
                full_size = QSize(*im.size)
                if orientation in (5, 6, 7, 8):
                    full_size.transpose()
                frame = _shrink_pil(im, max_side)
                if frame is im:
                    ImageOps.exif_transpose(im, in_place=True)
                elif orientation in EXIF_TRANSPOSE:  # a new image (16-bit source) has no EXIF to read
                    frame = frame.transpose(EXIF_TRANSPOSE[orientation])
            else:
                frame = im.copy()
            q = pil_to_qimage(_to_srgb(frame))
            budget -= q.sizeInBytes()
            if budget < 0 and frames:
                log.warning("Animation truncated at %d frames (memory cap)", i)
                break
            frames.append(q)
            durations.append(max(int(im.info.get("duration") or 100), 20))
        # EXIF sub-IFDs load lazily from the file, so read them before it closes.
        exif_rows, exif_bytes = exif_items(exif), _exif_bytes(exif)
    if len(frames) == 1 and full_size is None and max_side and max(frames[0].width(), frames[0].height()) > max_side:
        # An animation or multi-page file whose second frame did not fit the memory cap: one frame, shown like a
        # still (a screen copy, full resolution on zoom).
        full_size = frames[0].size()
        frames[0] = frames[0].scaled(max_side, max_side, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
    size = full_size or frames[0].size()
    meta = _image_items(fmt, size, len(frames), icc_rows) + exif_rows
    return DecodedImage(frames, durations, fmt, meta, exif_bytes, full_size)


def _decode_jxl(data: bytes, max_side: int | None = None) -> DecodedImage:
    """The first frame of a JPEG XL file. libjxl would decode every frame of an animation at once, before any
    memory cap could apply, so animations show their first picture (they are very rare)."""
    import imagecodecs

    size = jxl_size(data)
    if size is None:  # without a readable size, the pixel limit cannot be checked: never decode blind
        raise DecodeError("The JPEG XL file is damaged or unsupported.")
    check_pixels(*size)
    try:
        arr = imagecodecs.jpegxl_decode(data, index=0, numthreads=JXL_THREADS)
    except Exception as exc:  # imagecodecs raises its own error hierarchy
        raise DecodeError("The JPEG XL file is damaged or unsupported.") from exc
    if arr.ndim == 4:  # some builds return a stack of one frame
        arr = arr[0]
    full_size = QSize(arr.shape[1], arr.shape[0])
    frame = ndarray_to_qimage(arr, max_side)
    del arr  # free the full-size pixels before the caller sees the result
    return DecodedImage([frame], [100], "JPEG XL", _image_items("JPEG XL", full_size, 1), full_size=full_size)


def _decode_raw(data: bytes, ext: str, max_side: int | None = None) -> DecodedImage:
    import rawpy

    # From memory: with rawpy 0.28 as fast as from the path (12 MP NEF: 326 vs 371 ms at half size, 915 vs
    # 954 ms in full, measured 2026-11-05), and the protected helper never gets a path.
    try:
        with rawpy.imread(io.BytesIO(data)) as raw:
            sizes = raw.sizes
            check_pixels(sizes.width, sizes.height)
            check_pixels(sizes.raw_width, sizes.raw_height)
            # Half size skips demosaicing and is about 4x faster: use it when it still covers the screen.
            half = bool(max_side) and max(sizes.width, sizes.height) // 2 >= max_side
            rgb = raw.postprocess(use_camera_wb=True, output_bps=8, no_auto_bright=False, half_size=half)
    except (rawpy.LibRawError, OSError, ValueError) as exc:
        raise DecodeError("This camera RAW file is damaged or from an unsupported camera.") from exc
    # LibRaw's output is upright: flip 5 or 6 turns the sensor 90 degrees.
    full_size = QSize(sizes.width, sizes.height)
    if getattr(sizes, "flip", 0) in (5, 6):
        full_size.transpose()
    frame = ndarray_to_qimage(rgb, max_side)
    del rgb
    extra = [MetaItem("Image", "Sensor size", f"{sizes.raw_width} x {sizes.raw_height} px")]
    exif_rows: list[MetaItem] = []
    exif_bytes = None
    try:  # Most RAW formats are TIFF-based, so Pillow can usually read their EXIF.
        with Image.open(io.BytesIO(data), formats=("TIFF",)) as tiff:  # TIFF-based RAWs only; never another plugin
            exif = tiff.getexif()
            exif_rows, exif_bytes = exif_items(exif), _exif_bytes(exif)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        pass
    fmt = f"Camera RAW ({ext.lstrip('.').upper()})"
    return DecodedImage([frame], [0], fmt, _image_items(fmt, full_size, 1, extra) + exif_rows, exif_bytes,
                        full_size)


def _exif_bytes(exif: Image.Exif | None) -> bytes | None:
    """Serialise EXIF for Save as. Vendor quirks must never stop the image from opening."""
    if not exif:
        return None
    try:
        return exif.tobytes()
    except Exception:
        log.debug("EXIF could not be serialised; saved copies will omit it", exc_info=True)
        return None


PSD_EXTS = frozenset({".psd", ".psb"})
_PSD_MODES = {0: "Bitmap", 1: "Grayscale", 2: "Indexed", 3: "RGB", 4: "CMYK", 7: "Multichannel", 8: "Duotone",
              9: "Lab"}
NO_MERGED_IMAGE = ("Photoshop saved this file without a flattened image, so Media Reader x cannot show it. "
                   "To fix this, open the file in Photoshop, turn on Maximize PSD and PSB File Compatibility "
                   "in Preferences > File Handling, and save the file again.")


def psd_header_bytes(head: bytes) -> tuple[int, int, int, int] | None:
    """Width, height, bit depth and colour mode from the 26-byte PSD/PSB header, or None."""
    if len(head) < 26 or head[:4] != b"8BPS" or head[4:6] not in (b"\x00\x01", b"\x00\x02"):
        return None
    height, width, depth, mode = struct.unpack(">IIHH", head[14:26])
    return width, height, depth, mode


def psd_header(path: Path) -> tuple[int, int, int, int] | None:
    """``psd_header_bytes`` for a file."""
    with Path(path).open("rb") as f:
        return psd_header_bytes(f.read(26))


def open_psd(data: bytes):
    """Parse a PSD/PSB after checking its size from the header (psd-tools reads the whole file)."""
    from psd_tools import PSDImage

    header = psd_header_bytes(data[:26])
    if header is None:
        raise DecodeError("The file is damaged or is not a Photoshop file.")
    check_pixels(header[0], header[1])
    try:
        return PSDImage.open(io.BytesIO(data)), header
    except Exception as exc:  # psd-tools raises its own and struct errors on damaged files
        raise DecodeError("The Photoshop file is damaged or uses features that Media Reader x cannot read.") from exc


def _decode_psd(data: bytes, max_side: int | None = None) -> DecodedImage:
    """Show the flattened image Photoshop stores in the file (8, 16 or 32-bit, PSD or PSB).

    Layers are not composited here: that needs scipy and scikit-image (about 100 MB), and
    Photoshop saves the flattened image by default ("Maximize compatibility").
    """
    psd, (_w, _h, depth, mode) = open_psd(data)
    try:
        im = psd.topil() if psd.has_preview() else None  # converted to sRGB from the file's profile
    except Exception as exc:
        raise DecodeError("The Photoshop file is damaged or uses features that Media Reader x cannot read.") from exc
    if im is None:
        raise DecodeError(NO_MERGED_IMAGE)
    layers = sum(1 for layer in psd.descendants() if not layer.is_group())
    # psd-tools layers link to their parents (reference cycles): without a collection, a 161 MB
    # file kept 179 MB until Python's collector happened to run (measured). Free it now.
    del psd
    gc.collect()
    full_size = QSize(*im.size)
    frame = pil_to_qimage(_shrink_pil(im, max_side))
    fmt = "Photoshop (PSB)" if data[4:6] == b"\x00\x02" else "Photoshop (PSD)"
    extra = [MetaItem("Image", "Colour mode", f"{_PSD_MODES.get(mode, 'Unknown')}, {depth}-bit"),
             MetaItem("Image", "Layers", str(layers))]
    return DecodedImage([frame], [0], fmt, _image_items(fmt, full_size, 1, extra), full_size=full_size)


def load_svg(data: bytes) -> QSvgRenderer:
    """A renderer for the SVG or SVGZ ``data`` after the safety check in content.check_svg. Raises DecodeError."""
    try:
        checked = content.check_svg(data)
    except content.Refused as exc:
        raise DecodeError(str(exc)) from exc
    return QSvgRenderer(QByteArray(checked))


def _decode_svg(data: bytes) -> DecodedImage:
    renderer = load_svg(data)
    if not renderer.isValid():
        raise DecodeError("The SVG file is damaged or uses unsupported features.")
    base = renderer.defaultSize()
    if base.isEmpty():
        base = QSize(1024, 1024)
    # Rasterise at up to 2x for crisp zoom, capped to keep memory sensible.
    scale = min(2.0, 4096 / max(base.width(), base.height()))
    size = QSize(max(1, int(base.width() * scale)), max(1, int(base.height() * scale)))
    img = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    painter = QPainter(img)
    renderer.render(painter)
    painter.end()
    rows = _image_items("SVG (vector)", base, 1)
    return DecodedImage([img], [0], "SVG", rows)


def decode_bytes(data: bytes, ext: str, max_side: int | None = None) -> DecodedImage:
    """Decode the content of a file named ``*{ext}`` into display-ready frames and image metadata rows.

    Needs no path: the protected helper process (sandbox.py) runs this on bytes it was handed.
    ``max_side`` limits the long side of a still (a screen copy, see ``DecodedImage.reduced``).
    None decodes full resolution. Animations and SVG always decode as before.
    """
    register_plugins()
    kind = image_kind(ext)
    check_file_size(len(data))
    try:
        notice = None
        if kind is MediaKind.RAW:
            result = _decode_raw(data, ext, max_side)
        elif kind is MediaKind.SVG:
            result = _decode_svg(data)
        else:
            # The content picks the decoder, from the families the extension allows plus a notice otherwise.
            way = route_ext(ext, data[:content.HEAD_BYTES])
            notice = way.notice
            if way.family == "psd":
                result = _decode_psd(data, max_side)
            elif way.family == "jxl":
                result = _decode_jxl(data, max_side)
            else:
                result = _decode_pillow(data, way.family, max_side)
        result.notice = notice
    except DecodeError:
        raise
    except MemoryError as exc:
        raise DecodeError("Not enough memory to open this image.") from exc
    except OSError as exc:
        raise DecodeError("The file is damaged or cannot be read.") from exc
    except Exception as exc:
        log.exception("Unexpected decoder failure")
        raise DecodeError("Media Reader x cannot open this image. "
                          "To see why, select Help > Open log folder.") from exc
    return result


def read_file(path: Path) -> bytes:
    """The whole file, after the size limits. Raises DecodeError."""
    try:
        check_file_size(path.stat().st_size)
        return path.read_bytes()
    except OSError as exc:
        raise DecodeError("The file cannot be read. It may have been moved or you may lack access.") from exc


def decode(path: str | Path, max_side: int | None = None) -> DecodedImage:
    """Decode ``path`` in this process: ``decode_bytes`` plus the rows about the file itself.

    The app decodes through ``sandbox.decode``, which runs ``decode_bytes`` in a protected helper and
    falls back to this function only when no helper can start on the PC.
    """
    path = Path(path)
    image_kind(path.suffix)
    result = decode_bytes(read_file(path), path.suffix, max_side)
    try:
        result.metadata = file_items(path) + result.metadata
    except OSError as exc:  # the file can vanish between the pixel decode and this stat
        raise DecodeError("The file cannot be read. It may have been moved or you may lack access.") from exc
    return result
