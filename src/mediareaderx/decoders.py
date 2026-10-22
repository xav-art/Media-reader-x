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
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError
from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

from mediareaderx import config
from mediareaderx.formats import MediaKind, kind_of
from mediareaderx.metadata import MetaItem, exif_items, file_items

log = logging.getLogger(__name__)

_registered = False


class DecodeError(Exception):
    """Raised with a user-facing message when a file cannot be shown."""


@dataclass
class DecodedImage:
    frames: list[QImage]
    durations_ms: list[int]
    format_name: str
    metadata: list[MetaItem] = field(default_factory=list)
    exif_bytes: bytes | None = None  # original EXIF, reused by Save as
    full_size: QSize | None = None  # the picture's real size when the frames are a smaller screen copy

    @property
    def size(self) -> QSize:
        """The picture's real size (upright), even when the frames are a screen copy."""
        return self.full_size if self.full_size is not None else self.frames[0].size()

    @property
    def reduced(self) -> bool:
        return self.full_size is not None and self.full_size != self.frames[0].size()

    @property
    def animated(self) -> bool:
        return len(self.frames) > 1


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


def register_plugins() -> None:
    """Register optional Pillow plugins once (HEIF via libheif)."""
    global _registered
    if _registered:
        return
    import pillow_heif

    # AVIF is decoded by Pillow's native plugin; libheif only handles HEIC/HEIF.
    pillow_heif.register_heif_opener(thumbnails=False)
    Image.MAX_IMAGE_PIXELS = config.MAX_IMAGE_PIXELS
    _registered = True


def warm_up() -> None:
    """Load codec libraries and plugins ahead of the first real decode (about 200 ms)."""
    try:
        register_plugins()
        import imagecodecs  # noqa: F401  (JPEG XL)
        import rawpy  # noqa: F401  (LibRaw)

        buf = io.BytesIO()
        Image.new("RGB", (8, 8)).save(buf, "AVIF")
        with Image.open(io.BytesIO(buf.getvalue())) as im:
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


def jxl_size(data: bytes) -> tuple[int, int] | None:
    """Read width and height from a JPEG XL header without decoding (spec SizeHeader)."""
    if data[:2] == b"\xff\x0a":
        code = data
    elif data[:12] == b"\x00\x00\x00\x0cJXL \x0d\x0a\x87\x0a":
        code, pos = None, 12
        while pos + 8 <= len(data):  # ISO-BMFF boxes: find the codestream (jxlc or the first jxlp)
            size, kind = int.from_bytes(data[pos:pos + 4], "big"), data[pos + 4:pos + 8]
            header = 8
            if size == 1:
                size, header = int.from_bytes(data[pos + 8:pos + 16], "big"), 16
            elif size == 0:
                size = len(data) - pos
            if kind == b"jxlc":
                code = data[pos + header:pos + size]
                break
            if kind == b"jxlp":
                code = data[pos + header + 4:pos + size]
                break
            if size < header:
                return None
            pos += size
        if code is None or code[:2] != b"\xff\x0a":
            return None
    else:
        return None
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


def _decode_pillow(path: Path, max_side: int | None = None) -> DecodedImage:
    try:
        # Read into memory first, so the file is not held open (and locked) while decoding.
        im = Image.open(io.BytesIO(path.read_bytes()))
    except UnidentifiedImageError as exc:
        raise DecodeError("The file is damaged or is not a supported image.") from exc
    except Image.DecompressionBombError as exc:
        raise DecodeError("The image is too large to open safely.") from exc

    with im:
        check_pixels(*im.size)
        fmt = "JPEG (MPO)" if im.format == "MPO" else im.format or path.suffix.lstrip(".").upper()
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
    size = full_size or frames[0].size()
    meta = _image_items(fmt, size, len(frames), icc_rows) + exif_rows
    return DecodedImage(frames, durations, fmt, meta, exif_bytes, full_size)


def _decode_jxl(path: Path, max_side: int | None = None) -> DecodedImage:
    import imagecodecs

    data = path.read_bytes()
    size = jxl_size(data)
    if size is not None:
        check_pixels(*size)
    try:
        arr = imagecodecs.jpegxl_decode(data, numthreads=JXL_THREADS)
    except Exception as exc:  # imagecodecs raises its own error hierarchy
        raise DecodeError("The JPEG XL file is damaged or unsupported.") from exc
    # Animated JXL decodes to (frames, h, w, c); stills to (h, w[, c]).
    stack = list(arr[: config.MAX_ANIMATION_FRAMES]) if arr.ndim == 4 else [arr]
    if len(stack) > 1:
        frames = [ndarray_to_qimage(a) for a in stack]
        return DecodedImage(frames, [100] * len(frames), "JPEG XL",
                            _image_items("JPEG XL", frames[0].size(), len(frames)))
    full_size = QSize(stack[0].shape[1], stack[0].shape[0])
    frame = ndarray_to_qimage(stack[0], max_side)
    del arr, stack  # free the full-size pixels before the caller sees the result
    return DecodedImage([frame], [100], "JPEG XL", _image_items("JPEG XL", full_size, 1), full_size=full_size)


def _decode_raw(path: Path, max_side: int | None = None) -> DecodedImage:
    import rawpy

    # From the path, not from memory: LibRaw decodes a memory buffer at about half speed
    # (measured 1.5 s vs 0.65 s for a 12 MP NEF). The file stays open during the decode.
    try:
        with rawpy.imread(str(path)) as raw:
            sizes = raw.sizes
            check_pixels(sizes.width, sizes.height)
            # Half size skips demosaicing and is about 4x faster: use it when it still covers the screen.
            half = bool(max_side) and max(sizes.width, sizes.height) // 2 >= max_side
            rgb = raw.postprocess(use_camera_wb=True, output_bps=8, no_auto_bright=False, half_size=half)
    except (rawpy.LibRawError, OSError) as exc:
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
        with Image.open(path) as tiff:
            exif = tiff.getexif()
            exif_rows, exif_bytes = exif_items(exif), _exif_bytes(exif)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        pass
    fmt = f"Camera RAW ({path.suffix.lstrip('.').upper()})"
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


def psd_header(path: Path) -> tuple[int, int, int, int] | None:
    """Width, height, bit depth and colour mode from the 26-byte PSD/PSB header, or None."""
    with path.open("rb") as f:
        head = f.read(26)
    if len(head) < 26 or head[:4] != b"8BPS" or head[4:6] not in (b"\x00\x01", b"\x00\x02"):
        return None
    height, width, depth, mode = struct.unpack(">IIHH", head[14:26])
    return width, height, depth, mode


def open_psd(path: Path):
    """Parse a PSD/PSB after checking its size from the header (psd-tools reads the whole file)."""
    from psd_tools import PSDImage

    header = psd_header(path)
    if header is None:
        raise DecodeError("The file is damaged or is not a Photoshop file.")
    check_pixels(header[0], header[1])
    try:
        return PSDImage.open(path), header
    except Exception as exc:  # psd-tools raises its own and struct errors on damaged files
        raise DecodeError("The Photoshop file is damaged or uses features that Media Reader x cannot read.") from exc


def _decode_psd(path: Path, max_side: int | None = None) -> DecodedImage:
    """Show the flattened image Photoshop stores in the file (8, 16 or 32-bit, PSD or PSB).

    Layers are not composited here: that needs scipy and scikit-image (about 100 MB), and
    Photoshop saves the flattened image by default ("Maximize compatibility").
    """
    psd, (_w, _h, depth, mode) = open_psd(path)
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
    fmt = "Photoshop (PSB)" if path.suffix.lower() == ".psb" else "Photoshop (PSD)"
    extra = [MetaItem("Image", "Colour mode", f"{_PSD_MODES.get(mode, 'Unknown')}, {depth}-bit"),
             MetaItem("Image", "Layers", str(layers))]
    return DecodedImage([frame], [0], fmt, _image_items(fmt, full_size, 1, extra), full_size=full_size)


def _decode_svg(path: Path) -> DecodedImage:
    renderer = QSvgRenderer(QByteArray(path.read_bytes()))
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


def decode(path: str | Path, max_side: int | None = None) -> DecodedImage:
    """Decode ``path`` into display-ready frames plus metadata rows.

    ``max_side`` limits the long side of a still (a screen copy, see ``DecodedImage.reduced``).
    None decodes full resolution. Animations and SVG always decode as before.
    """
    register_plugins()
    path = Path(path)
    kind = kind_of(path)
    if kind is None or kind is MediaKind.VIDEO:
        raise DecodeError(f"'{path.suffix}' is not a supported image type.")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise DecodeError("The file cannot be read. It may have been moved or you may lack access.") from exc
    if size > config.MAX_IMAGE_FILE_BYTES:
        raise DecodeError("The file is larger than the 1 GB safety limit for images.")
    if size == 0:
        raise DecodeError("The file is empty.")

    try:
        if kind is MediaKind.RAW:
            result = _decode_raw(path, max_side)
        elif kind is MediaKind.SVG:
            result = _decode_svg(path)
        elif path.suffix.lower() in PSD_EXTS:
            result = _decode_psd(path, max_side)
        elif path.suffix.lower() == ".jxl":
            result = _decode_jxl(path, max_side)
        else:
            result = _decode_pillow(path, max_side)
        # Inside the try: the file can vanish between the pixel decode and this stat.
        result.metadata = file_items(path) + result.metadata
    except DecodeError:
        raise
    except MemoryError as exc:
        raise DecodeError("Not enough memory to open this image.") from exc
    except OSError as exc:
        raise DecodeError("The file cannot be read. It may have been moved or you may lack access.") from exc
    except Exception as exc:
        log.exception("Unexpected decoder failure")
        raise DecodeError("Media Reader x cannot open this image. "
                          "To see why, select Help > Open log folder.") from exc
    return result
