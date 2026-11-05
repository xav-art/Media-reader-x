"""Single source of truth for the file extensions Media Reader x opens.

The installer's file-association list is generated from this module
(see tools/gen_iss_associations.py), so add new formats here only.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path


class MediaKind(Enum):
    IMAGE = "image"
    RAW = "raw"
    SVG = "svg"
    VIDEO = "video"


IMAGE_EXTS = frozenset({
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".apng", ".gif", ".bmp", ".dib",
    ".tif", ".tiff", ".webp", ".ico", ".heic", ".heif", ".hif", ".avif", ".jxl",
    ".psd", ".psb", ".tga", ".ppm", ".pgm", ".pbm", ".pnm", ".dds", ".qoi",
})

RAW_EXTS = frozenset({
    ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".dng",
    ".raf", ".orf", ".rw2", ".pef", ".srw", ".x3f", ".3fr", ".iiq", ".erf",
    ".mef", ".mos", ".mrw", ".kdc", ".dcr", ".rwl",
})

SVG_EXTS = frozenset({".svg", ".svgz"})

VIDEO_EXTS = frozenset({
    ".mp4", ".m4v", ".mov", ".qt", ".mkv", ".webm", ".avi", ".wmv", ".mts",
    ".m2ts", ".ts", ".3gp", ".3g2", ".flv", ".ogv", ".mpg", ".mpeg",
})

ALL_EXTS = IMAGE_EXTS | RAW_EXTS | SVG_EXTS | VIDEO_EXTS

# Extensions Windows cannot open out of the box (or only with paid Store
# extensions). The installer offers "Open with" registration for these.
ASSOCIATION_EXTS = frozenset({
    ".heic", ".heif", ".hif", ".avif", ".jxl", ".psd", ".psb", ".qoi", ".webp",
    ".svg", ".svgz", ".mkv", ".webm", ".mov",
} | RAW_EXTS)


# The content families each extension normally holds (see content.py). A file whose content is another
# supported family still opens, with the decoder for its real content and a notice.
IMAGE_FAMILIES: dict[str, tuple[str, ...]] = {
    ".jpg": ("jpeg",), ".jpeg": ("jpeg",), ".jpe": ("jpeg",), ".jfif": ("jpeg",),
    ".png": ("png",), ".apng": ("png",), ".gif": ("gif",), ".bmp": ("bmp", "dib"), ".dib": ("bmp", "dib"),
    ".tif": ("tiff",), ".tiff": ("tiff",), ".webp": ("webp",), ".ico": ("ico",),
    ".heic": ("heic",), ".heif": ("heic", "avif"), ".hif": ("heic", "avif"), ".avif": ("avif",),
    ".jxl": ("jxl",), ".psd": ("psd",), ".psb": ("psd",), ".tga": ("tga",),
    ".ppm": ("pnm",), ".pgm": ("pnm",), ".pbm": ("pnm",), ".pnm": ("pnm",), ".dds": ("dds",), ".qoi": ("qoi",),
}

VIDEO_FAMILIES: dict[str, tuple[str, ...]] = {
    ".mp4": ("isobmff",), ".m4v": ("isobmff",), ".mov": ("isobmff",), ".qt": ("isobmff",),
    ".3gp": ("isobmff",), ".3g2": ("isobmff",), ".mkv": ("matroska",), ".webm": ("matroska",),
    ".avi": ("avi",), ".wmv": ("asf",), ".ts": ("mpegts",), ".mts": ("mpegts",), ".m2ts": ("mpegts",),
    ".flv": ("flv",), ".ogv": ("ogg",), ".mpg": ("mpegps", "mpegts"), ".mpeg": ("mpegps", "mpegts"),
}


def kind_of(path: str | Path) -> MediaKind | None:
    """Return the media kind for ``path`` based on its extension, or None."""
    ext = Path(path).suffix.lower()
    if ext in RAW_EXTS:
        return MediaKind.RAW
    if ext in SVG_EXTS:
        return MediaKind.SVG
    if ext in VIDEO_EXTS:
        return MediaKind.VIDEO
    if ext in IMAGE_EXTS:
        return MediaKind.IMAGE
    return None


def dialog_filter() -> str:
    """Build a Qt file-dialog filter string covering every supported family."""

    def pattern(exts: frozenset[str]) -> str:
        return " ".join(f"*{e}" for e in sorted(exts))

    return ";;".join([
        f"All supported media ({pattern(ALL_EXTS)})",
        f"Images ({pattern(IMAGE_EXTS | SVG_EXTS)})",
        f"Camera RAW ({pattern(RAW_EXTS)})",
        f"Video ({pattern(VIDEO_EXTS)})",
        "All files (*)",
    ])
