"""Add the native libraries inside the Python wheels to the CycloneDX SBOM.

cyclonedx-py lists only the Python packages. The code that parses untrusted files is native: libheif,
libde265, LibRaw, libjxl, dav1d, libwebp, FFmpeg, Qt. This script reads each library's version from the
build environment at build time (never a hard-coded guess), adds it as a component with a CPE id so
vulnerability scanners (grype, Dependency-Track, Defender) can match CVEs, and links it to the wheel that
ships it. Libraries compiled into Qt expose no version and come from packaging/sbom-declared.json, which
is checked against the bundled Qt version. Any version that cannot be read fails the build.

Usage: python tools/sbom_native.py <sbom.cdx.json>   (rewrites the file in place)
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class VersionError(RuntimeError):
    pass


def _match(pattern: str, text: str, what: str) -> str:
    found = re.search(pattern, text)
    if not found:
        raise VersionError(f"cannot read the {what} version from {text!r}")
    return found.group(1)


def _heif() -> list[dict]:
    import pillow_heif

    info = pillow_heif.libheif_info()
    x265 = info["encoders"]["x265"]
    return [
        _component("libheif", _match(r"^(\d+\.\d+\.\d+)$", info["libheif"], "libheif"), ["struktur:libheif"],
                   "pillow-heif", "HEIC/HEIF container parsing"),
        _component("libde265", _match(r"version (\d+\.\d+\.\d+)", info["decoders"]["libde265"], "libde265"),
                   ["struktur:libde265"], "pillow-heif", "HEVC picture decoding (HEIC)"),
        _component("x265", _match(r"\((\d+\.\d+)", x265, "x265"), ["multicorewareinc:x265"], "pillow-heif",
                   "HEVC encoder linked by libheif; never called by Media Reader x",
                   {"build": _match(r"\(([^)]+)\)", x265, "x265 build")}),
    ]


def _pillow() -> list[dict]:
    from PIL import _avif, features

    def ver(name: str) -> str:
        value = features.version(name)
        if not value:
            raise VersionError(f"Pillow reports no {name} version")
        return value

    codecs = _avif.codec_versions()
    return [
        _component("libjpeg-turbo", ver("libjpeg_turbo"), ["libjpeg-turbo:libjpeg-turbo", "d.r.commander:libjpeg-turbo"],
                   "pillow", "JPEG decoding"),
        _component("libwebp", ver("webp"), ["webmproject:libwebp"], "pillow", "WebP decoding"),
        _component("libavif", ver("avif"), ["aomedia:libavif"], "pillow", "AVIF container parsing"),
        _component("dav1d", _match(r"dav1d \[dec\]:(\d+\.\d+\.\d+)", codecs, "dav1d"), ["videolan:dav1d"],
                   "pillow", "AV1 picture decoding (AVIF)"),
        _component("libaom", _match(r"aom \[enc\]:(\d+\.\d+\.\d+)", codecs, "libaom"), ["aomedia:aomedia"],
                   "pillow", "AV1 encoder in the AVIF module; never called by Media Reader x"),
        _component("libtiff", ver("libtiff"), ["libtiff:libtiff"], "pillow", "TIFF decoding, RAW EXIF"),
        _component("lcms2", ver("littlecms2"), ["littlecms:little_cms_color_engine"], "pillow", "colour profiles"),
        _component("freetype", ver("freetype2"), ["freetype:freetype"], "pillow", "linked, unused by the app"),
        _component("openjpeg", ver("jpg_2000"), ["uclouvain:openjpeg"], "pillow",
                   "linked; unreachable (JPEG 2000 is not a supported format)"),
        _component("zlib-ng", ver("zlib_ng"), [], "pillow", "PNG and TIFF inflate (no CPE exists in NVD)"),
    ]


def _imagecodecs() -> list[dict]:
    import imagecodecs

    return [
        _component("libjxl", _match(r"libjxl (\d+\.\d+\.\d+)", imagecodecs.jpegxl_version(), "libjxl"),
                   ["libjxl_project:libjxl"], "imagecodecs", "JPEG XL decoding",
                   {"also-contains": "highway, a copy of lcms2 (versions not exposed)"}),
        _component("brotli", _match(r"brotli (\d+\.\d+\.\d+)", imagecodecs.brotli_version(), "brotli"),
                   ["google:brotli"], "imagecodecs", "used by libjxl; version from the same imagecodecs build"),
    ]


def _rawpy() -> list[dict]:
    import rawpy

    libraw = ".".join(str(p) for p in rawpy.libraw_version)
    dll = Path(rawpy.__file__).parent / "raw_r.dll"
    zlib_version = _match(r"inflate (\d+\.\d+\.\d+) Copyright", dll.read_bytes().decode("latin-1"), "LibRaw zlib")
    return [
        _component("libraw", libraw, ["libraw:libraw"], "rawpy", "camera RAW decoding"),
        _component("zlib", zlib_version, ["zlib:zlib"], "rawpy", "inside raw_r.dll (compressed DNG)",
                   ref="zlib-rawpy"),
    ]


def _file_version(dll: Path) -> str:
    import pefile

    pe = pefile.PE(str(dll), fast_load=True)
    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
    for info in getattr(pe, "FileInfo", [[]])[0]:
        for table in getattr(info, "StringTable", []):
            value = table.entries.get(b"ProductVersion")
            if value:
                return value.decode()
    raise VersionError(f"{dll.name} has no ProductVersion")


def _qt() -> list[dict]:
    import PySide6
    from PySide6.QtCore import qVersion

    qt = qVersion()
    pyside = Path(PySide6.__file__).parent
    dlls = sorted(pyside.glob("avcodec-*.dll"))
    if len(dlls) != 1:
        raise VersionError(f"expected one avcodec DLL in {pyside}, found {[d.name for d in dlls]}")
    os.add_dll_directory(str(pyside))
    avcodec = ctypes.CDLL(str(dlls[0]))
    avcodec.avcodec_configuration.restype = ctypes.c_char_p
    configure = avcodec.avcodec_configuration().decode()
    qjpeg = (pyside / "plugins" / "imageformats" / "qjpeg.dll").read_bytes().decode("latin-1")
    avformat = sorted(pyside.glob("avformat-*.dll"))[0].read_bytes().decode("latin-1")
    out = [
        _component("qt", qt, ["qt:qt"], "PySide6-Essentials", "GUI, SVG rendering, multimedia"),
        _component("ffmpeg", _match(r"^(\d+\.\d+(?:\.\d+)?)$", _file_version(dlls[0]), "FFmpeg"), ["ffmpeg:ffmpeg"],
                   "PySide6-Addons", "video demuxing and decoding (Qt Multimedia)",
                   {"configure": configure[:2000]}),
        _component("libjpeg-turbo", _match(r"libjpeg-turbo version (\d+\.\d+\.\d+)", qjpeg, "qjpeg libjpeg-turbo"),
                   ["libjpeg-turbo:libjpeg-turbo", "d.r.commander:libjpeg-turbo"], "PySide6-Essentials",
                   "Qt JPEG plugin (JPEG pictures inside SVG files)", ref="libjpeg-turbo-qt"),
        _component("zlib", _match(r"inflate (\d+\.\d+\.\d+) Copyright", avformat, "FFmpeg zlib"), ["zlib:zlib"],
                   "PySide6-Addons", "inside FFmpeg (compressed MP4/MKV headers, PNG in video)", ref="zlib-ffmpeg"),
    ]
    declared = json.loads((ROOT / "packaging" / "sbom-declared.json").read_text(encoding="utf-8"))
    if declared["qt_version"] != qt:
        raise VersionError(f"packaging/sbom-declared.json is for Qt {declared['qt_version']}, the build has Qt {qt}: "
                           "update it from Qt's qt_attribution.json files")
    for item in declared["components"]:
        out.append(_component(item["name"], item["version"], item["cpe"], "PySide6-Essentials", item["in"],
                              {"version-source": f"declared from Qt {qt} attribution ({declared['source']})"},
                              ref=f"{item['name']}-qt"))
    return out


def _runtime() -> list[dict]:
    import numpy
    import pyexpat

    blas = numpy.__config__.CONFIG["Build Dependencies"]["blas"]
    return [
        _component("cpython", platform.python_version(), ["python:python"], None,
                   "Python runtime; also contains libffi, xz, bzip2 (versions follow the CPython release)"),
        _component("libexpat", _match(r"expat_(\d+\.\d+\.\d+)", pyexpat.EXPAT_VERSION, "expat"),
                   ["libexpat_project:libexpat"], None, "XML parser in CPython (pyexpat)"),
        _component("openblas", ".".join(blas["version"].split(".")[:3]), ["openblas_project:openblas"], "numpy",
                   "linear algebra in numpy; not reached by file content", {"build": blas["version"]}),
    ]


def _component(name: str, version: str, cpes: list[str], wheel: str | None, use: str,
               props: dict | None = None, ref: str | None = None) -> dict:
    comp = {
        "type": "library",
        "bom-ref": f"native:{ref or name}",
        "name": name,
        "version": version,
        "purl": f"pkg:generic/{name}@{version}",
        "description": use,
        "properties": [{"name": "mediareaderx:bundled-by", "value": wheel or "Python runtime"}],
    }
    if cpes:
        comp["cpe"] = f"cpe:2.3:a:{cpes[0]}:{version}:*:*:*:*:*:*:*"
        for extra in cpes[1:]:
            comp["properties"].append({"name": "mediareaderx:alternative-cpe",
                                       "value": f"cpe:2.3:a:{extra}:{version}:*:*:*:*:*:*:*"})
    for key, value in (props or {}).items():
        comp["properties"].append({"name": f"mediareaderx:{key}", "value": value})
    comp["_wheel"] = wheel
    return comp


def main(path: Path) -> None:
    bom = json.loads(path.read_text(encoding="utf-8"))
    native = _heif() + _pillow() + _imagecodecs() + _rawpy() + _qt() + _runtime()
    wheels = {c["name"].lower(): c["bom-ref"] for c in bom["components"]}
    deps = {d["ref"]: d for d in bom.setdefault("dependencies", [])}
    for comp in native:
        wheel = comp.pop("_wheel")
        if wheel is not None:
            owner = wheels.get(wheel.lower())
            if owner is None:
                raise VersionError(f"{comp['name']}: the wheel {wheel} is not in the SBOM")
            deps.setdefault(owner, {"ref": owner}).setdefault("dependsOn", []).append(comp["bom-ref"])
        deps[comp["bom-ref"]] = {"ref": comp["bom-ref"]}
    bom["components"].extend(native)
    bom["dependencies"] = list(deps.values())
    path.write_text(json.dumps(bom, indent=2) + "\n", encoding="utf-8")
    print(f"SBOM: added {len(native)} native components")
    for comp in native:
        print(f"  {comp['name']:<14} {comp['version']:<10} {comp.get('cpe', '(no CPE)')}")


if __name__ == "__main__":
    try:
        main(Path(sys.argv[1]))
    except VersionError as exc:
        print(f"SBOM error: {exc}", file=sys.stderr)
        sys.exit(1)
