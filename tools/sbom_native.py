"""Add the native libraries inside the Python wheels to the CycloneDX SBOM.

cyclonedx-py lists only the Python packages. The code that parses untrusted files is native: libheif,
libde265, LibRaw, libjxl, dav1d, libwebp, FFmpeg, Qt. This script reads each library's version from the
build environment at build time (never a hard-coded guess), adds it as a component with a CPE id so
vulnerability scanners (grype, Dependency-Track, Defender) can match CVEs, and links it to the wheel that
ships it. Libraries compiled into Qt expose no version and come from packaging/sbom-declared.json, which
is checked against the bundled Qt version. Any version that cannot be read fails the build.

Each component also carries its SPDX licence expression, a copyright line and a version-pinned source
URL. tools/gen_notices.py reads the same list (native_components()) for THIRD_PARTY_NOTICES.txt, so the
SBOM and the notices cannot disagree.

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
    heif = _match(r"^(\d+\.\d+\.\d+)$", info["libheif"], "libheif")
    de265 = _match(r"version (\d+\.\d+\.\d+)", info["decoders"]["libde265"], "libde265")
    x265_version = _match(r"\((\d+\.\d+)", x265, "x265")
    return [
        _component("libheif", heif, ["struktur:libheif"], "pillow-heif", "HEIC/HEIF container parsing",
                   lic="LGPL-3.0-or-later",
                   copyright="Copyright (c) 2017-2020 Struktur AG; Copyright (c) 2017-2026 Dirk Farin",
                   source=f"https://github.com/strukturag/libheif/releases/download/v{heif}/libheif-{heif}.tar.gz"),
        _component("libde265", de265, ["struktur:libde265"], "pillow-heif", "HEVC picture decoding (HEIC)",
                   lic="LGPL-3.0-or-later",
                   copyright="Copyright (c) 2013-2014 Struktur AG; Copyright (c) 2013-2026 Dirk Farin",
                   source=f"https://github.com/strukturag/libde265/releases/download/v{de265}/libde265-{de265}.tar.gz"),
        _component("x265", x265_version, ["multicorewareinc:x265"], "pillow-heif",
                   "HEVC encoder in its own DLL (libx265), imported by the libheif DLL. Never called: "
                   "Media Reader x removes Pillow's HEIF writer at startup",
                   {"build": _match(r"\(([^)]+)\)", x265, "x265 build")},
                   lic="GPL-2.0-or-later", copyright="Copyright (C) 2013-2020 MulticoreWare, Inc",
                   source=f"https://github.com/Multicorewareinc/x265/releases/download/{x265_version}/"
                          f"x265_{x265_version}.tar.gz"),
    ]


def _mingw() -> list[dict]:
    """The GCC runtime DLLs that the MSYS2-built HEIF DLLs import (shipped in the pillow-heif wheel)."""
    import pillow_heif

    libs = Path(pillow_heif.__file__).resolve().parents[1]
    dlls = sorted(libs.glob("libgcc_s_seh-1*.dll"))
    if len(dlls) != 1:
        raise VersionError(f"expected one libgcc_s_seh-1 DLL in {libs}, found {[d.name for d in dlls]}")
    found = re.findall(rb"GCC: \([^)]*\) (\d+\.\d+\.\d+)", dlls[0].read_bytes())
    if not found:
        raise VersionError(f"cannot read the GCC version from {dlls[0].name}")
    gcc = max((v.decode() for v in found), key=lambda v: tuple(int(p) for p in v.split(".")))
    return [
        _component("gcc-runtime", gcc, [], "pillow-heif",
                   "libgcc_s_seh-1.dll and libstdc++-6.dll (MSYS2 GCC), the C and C++ runtime of the HEIF DLLs",
                   lic="GPL-3.0-or-later WITH GCC-exception-3.1",
                   copyright="Copyright (C) Free Software Foundation, Inc.",
                   source=f"https://ftp.gnu.org/gnu/gcc/gcc-{gcc}/gcc-{gcc}.tar.xz"),
    ]


def _pillow() -> list[dict]:
    from PIL import _avif, features

    def ver(name: str) -> str:
        value = features.version(name)
        if not value:
            raise VersionError(f"Pillow reports no {name} version")
        return value

    codecs = _avif.codec_versions()
    jpeg, webp, avif = ver("libjpeg_turbo"), ver("webp"), ver("avif")
    dav1d = _match(r"dav1d \[dec\]:(\d+\.\d+\.\d+)", codecs, "dav1d")
    aom = _match(r"aom \[enc\]:(\d+\.\d+\.\d+)", codecs, "libaom")
    tiff, lcms, freetype, openjpeg, zlib_ng = (ver("libtiff"), ver("littlecms2"), ver("freetype2"), ver("jpg_2000"),
                                               ver("zlib_ng"))
    return [
        _component("libjpeg-turbo", jpeg, ["libjpeg-turbo:libjpeg-turbo", "d.r.commander:libjpeg-turbo"],
                   "pillow", "JPEG decoding", lic=JPEG_TURBO_LICENSE, copyright=JPEG_TURBO_COPYRIGHT,
                   source=f"https://github.com/libjpeg-turbo/libjpeg-turbo/releases/download/{jpeg}/"
                          f"libjpeg-turbo-{jpeg}.tar.gz"),
        _component("libwebp", webp, ["webmproject:libwebp"], "pillow", "WebP decoding",
                   lic="BSD-3-Clause", copyright="Copyright (c) 2010, Google Inc.",
                   source=f"https://github.com/webmproject/libwebp/archive/refs/tags/v{webp}.tar.gz"),
        _component("libavif", avif, ["aomedia:libavif"], "pillow", "AVIF container parsing",
                   lic="BSD-2-Clause", copyright="Copyright 2019 Joe Drago",
                   source=f"https://github.com/AOMediaCodec/libavif/archive/refs/tags/v{avif}.tar.gz"),
        _component("dav1d", dav1d, ["videolan:dav1d"], "pillow", "AV1 picture decoding (AVIF)",
                   lic="BSD-2-Clause", copyright="Copyright (c) 2018-2025, VideoLAN and dav1d authors",
                   source=f"https://github.com/videolan/dav1d/archive/refs/tags/{dav1d}.tar.gz"),
        _component("libaom", aom, ["aomedia:aomedia"], "pillow",
                   "AV1 encoder in the AVIF module; never called by Media Reader x",
                   lic="BSD-2-Clause", copyright="Copyright (c) 2016, Alliance for Open Media",
                   source=f"https://storage.googleapis.com/aom-releases/libaom-{aom}.tar.gz"),
        _component("libtiff", tiff, ["libtiff:libtiff"], "pillow", "TIFF decoding, RAW EXIF",
                   lic="libtiff",
                   copyright="Copyright (c) 1988-1997 Sam Leffler; Copyright (c) 1991-1997 Silicon Graphics, Inc.",
                   source=f"https://download.osgeo.org/libtiff/tiff-{tiff}.tar.gz"),
        _component("lcms2", lcms, ["littlecms:little_cms_color_engine"], "pillow", "colour profiles",
                   lic="MIT", copyright="Copyright (c) 1998-2023 Marti Maria Saguer",
                   source=f"https://github.com/mm2/Little-CMS/archive/refs/tags/lcms{lcms}.tar.gz"),
        _component("freetype", freetype, ["freetype:freetype"], "pillow", "linked, unused by the app",
                   lic=FREETYPE_LICENSE, copyright=FREETYPE_COPYRIGHT,
                   source=f"https://download.savannah.gnu.org/releases/freetype/freetype-{freetype}.tar.xz"),
        _component("openjpeg", openjpeg, ["uclouvain:openjpeg"], "pillow",
                   "linked; unreachable (JPEG 2000 is not a supported format)",
                   lic="BSD-2-Clause",
                   copyright="Copyright (c) 2002-2014, Universite catholique de Louvain (UCL), Belgium, and others",
                   source=f"https://github.com/uclouvain/openjpeg/archive/refs/tags/v{openjpeg}.tar.gz"),
        _component("zlib-ng", zlib_ng, [], "pillow", "PNG and TIFF inflate (no CPE exists in NVD)",
                   lic="Zlib", copyright="(C) 1995-2024 Jean-loup Gailly and Mark Adler; zlib-ng contributors",
                   source=f"https://github.com/zlib-ng/zlib-ng/archive/refs/tags/{zlib_ng}.tar.gz"),
    ]


def _imagecodecs() -> list[dict]:
    import imagecodecs

    jxl = _match(r"libjxl (\d+\.\d+\.\d+)", imagecodecs.jpegxl_version(), "libjxl")
    brotli = _match(r"brotli (\d+\.\d+\.\d+)", imagecodecs.brotli_version(), "brotli")
    return [
        _component("libjxl", jxl, ["libjxl_project:libjxl"], "imagecodecs", "JPEG XL decoding",
                   {"also-contains": "highway (Apache-2.0 OR BSD-3-Clause), a copy of lcms2 (versions not exposed)"},
                   lic="BSD-3-Clause", copyright="Copyright (c) the JPEG XL Project Authors",
                   source=f"https://github.com/libjxl/libjxl/archive/refs/tags/v{jxl}.tar.gz"),
        _component("brotli", brotli, ["google:brotli"], "imagecodecs",
                   "used by libjxl; version from the same imagecodecs build",
                   lic="MIT", copyright="Copyright (c) 2009, 2010, 2013-2016 by the Brotli Authors",
                   source=f"https://github.com/google/brotli/archive/refs/tags/v{brotli}.tar.gz"),
    ]


def _rawpy() -> list[dict]:
    import rawpy

    libraw = ".".join(str(p) for p in rawpy.libraw_version)
    dll = Path(rawpy.__file__).parent / "raw_r.dll"
    zlib_version = _match(r"inflate (\d+\.\d+\.\d+) Copyright", dll.read_bytes().decode("latin-1"), "LibRaw zlib")
    return [
        _component("libraw", libraw, ["libraw:libraw"], "rawpy", "camera RAW decoding (raw_r.dll)",
                   lic="LGPL-2.1-only OR CDDL-1.0",
                   copyright="Copyright (C) 2008-2025 LibRaw LLC; Copyright 1997-2018 Dave Coffin",
                   source=f"https://github.com/LibRaw/LibRaw/archive/refs/tags/{libraw}.tar.gz"),
        _component("zlib", zlib_version, ["zlib:zlib"], "rawpy", "inside raw_r.dll (compressed DNG)",
                   ref="zlib-rawpy", lic="Zlib", copyright=ZLIB_COPYRIGHT, source=_zlib_source(zlib_version)),
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
    ffmpeg = _match(r"^(\d+\.\d+(?:\.\d+)?)$", _file_version(dlls[0]), "FFmpeg")
    zlib_version = _match(r"inflate (\d+\.\d+\.\d+) Copyright", avformat, "FFmpeg zlib")
    qt_source = qt_source_url(qt)
    out = [
        _component("qt", qt, ["qt:qt"], "PySide6-Essentials", "GUI, SVG rendering, multimedia",
                   lic="LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only",
                   copyright="Copyright (C) The Qt Company Ltd. and other contributors", source=qt_source),
        _component("ffmpeg", ffmpeg, ["ffmpeg:ffmpeg"],
                   "PySide6-Addons", "video demuxing and decoding (Qt Multimedia)",
                   {"configure": configure[:2000]},
                   lic="LGPL-2.1-or-later", copyright="Copyright (C) the FFmpeg developers",
                   source=f"https://ffmpeg.org/releases/ffmpeg-{ffmpeg}.tar.xz"),
        _component("libjpeg-turbo", _match(r"libjpeg-turbo version (\d+\.\d+\.\d+)", qjpeg, "qjpeg libjpeg-turbo"),
                   ["libjpeg-turbo:libjpeg-turbo", "d.r.commander:libjpeg-turbo"], "PySide6-Essentials",
                   "Qt JPEG plugin (JPEG pictures inside SVG files)", ref="libjpeg-turbo-qt",
                   lic=JPEG_TURBO_LICENSE, copyright=JPEG_TURBO_COPYRIGHT,
                   source=f"{qt_source} (qtbase/src/3rdparty/libjpeg)"),
        _component("zlib", zlib_version, ["zlib:zlib"], "PySide6-Addons",
                   "inside FFmpeg (compressed MP4/MKV headers, PNG in video)", ref="zlib-ffmpeg",
                   lic="Zlib", copyright=ZLIB_COPYRIGHT, source=_zlib_source(zlib_version)),
    ]
    declared = json.loads((ROOT / "packaging" / "sbom-declared.json").read_text(encoding="utf-8"))
    if declared["qt_version"] != qt:
        raise VersionError(f"packaging/sbom-declared.json is for Qt {declared['qt_version']}, the build has Qt {qt}: "
                           "update it from Qt's qt_attribution.json files")
    for item in declared["components"]:
        out.append(_component(item["name"], item["version"], item["cpe"], "PySide6-Essentials", item["in"],
                              {"version-source": f"declared from Qt {qt} attribution ({declared['source']})"},
                              ref=f"{item['name']}-qt", lic=item["license"], copyright=item["copyright"],
                              source=f"{qt_source} (qtbase/src/3rdparty)"))
    return out


def qt_source_url(version: str) -> str:
    """Qt's official source archive for one release (LGPLv3 allows a link to it)."""
    minor = ".".join(version.split(".")[:2])
    return f"https://download.qt.io/official_releases/qt/{minor}/{version}/single/qt-everywhere-src-{version}.tar.xz"


def _zlib_source(version: str) -> str:
    return f"https://github.com/madler/zlib/releases/download/v{version}/zlib-{version}.tar.gz"


def _runtime() -> list[dict]:
    import numpy
    import pyexpat

    blas = numpy.__config__.CONFIG["Build Dependencies"]["blas"]
    python = platform.python_version()
    expat = _match(r"expat_(\d+\.\d+\.\d+)", pyexpat.EXPAT_VERSION, "expat")
    openblas = ".".join(blas["version"].split(".")[:3])
    return [
        _component("cpython", python, ["python:python"], None,
                   "Python runtime; also contains libffi, xz, bzip2 (versions follow the CPython release)",
                   lic="PSF-2.0", copyright="Copyright (c) 2001 Python Software Foundation. All Rights Reserved.",
                   source=f"https://www.python.org/ftp/python/{python}/Python-{python}.tar.xz"),
        _component("libexpat", expat, ["libexpat_project:libexpat"], None, "XML parser in CPython (pyexpat)",
                   lic="MIT",
                   copyright="Copyright (c) 1998-2000 Thai Open Source Software Center Ltd and Clark Cooper; "
                             "Copyright (c) 2001 and later, Expat maintainers",
                   source=f"https://github.com/libexpat/libexpat/releases/download/R_{expat.replace('.', '_')}/"
                          f"expat-{expat}.tar.xz"),
        _component("openblas", openblas, ["openblas_project:openblas"], "numpy",
                   "linear algebra in numpy; not reached by file content", {"build": blas["version"]},
                   lic="BSD-3-Clause", copyright="Copyright (c) 2011-2014, The OpenBLAS Project",
                   source=f"https://github.com/OpenMathLib/OpenBLAS/releases/download/v{openblas}/"
                          f"OpenBLAS-{openblas}.tar.gz"),
    ]


# Shared by the two libjpeg-turbo copies (Pillow and Qt's JPEG plugin) and the two zlib copies.
JPEG_TURBO_LICENSE = "IJG AND BSD-3-Clause AND Zlib"
JPEG_TURBO_COPYRIGHT = ("Copyright (C) 2009-2026 D. R. Commander and others; copyright (C) 1991-2020 "
                        "Thomas G. Lane, Guido Vollbeding. Based in part on the work of the Independent JPEG Group")
FREETYPE_LICENSE = "FTL OR GPL-2.0-or-later"
FREETYPE_COPYRIGHT = "Copyright (C) 1996-2002, 2006 David Turner, Robert Wilhelm, and Werner Lemberg"
ZLIB_COPYRIGHT = "Copyright (C) Jean-loup Gailly and Mark Adler"


def _component(name: str, version: str, cpes: list[str], wheel: str | None, use: str,
               props: dict | None = None, ref: str | None = None, *, lic: str, copyright: str,
               source: str) -> dict:
    """One native library. lic is an SPDX licence expression; source is a version-pinned URL, optionally
    followed by a note in parentheses (for example the folder inside a bigger source archive)."""
    url, _, note = source.partition(" ")
    reference = {"type": "source-distribution", "url": url}
    if note:
        reference["comment"] = note.strip("()")
    comp = {
        "type": "library",
        "bom-ref": f"native:{ref or name}",
        "name": name,
        "version": version,
        "purl": f"pkg:generic/{name}@{version}",
        "description": use,
        "licenses": [{"expression": lic}] if " " in lic else [{"license": {"id": lic}}],
        "copyright": copyright,
        "externalReferences": [reference],
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


def native_components() -> list[dict]:
    """Every native library in the bundle, with "_wheel" still set (the wheel that ships it, or None)."""
    return _heif() + _mingw() + _pillow() + _imagecodecs() + _rawpy() + _qt() + _runtime()


def main(path: Path) -> None:
    bom = json.loads(path.read_text(encoding="utf-8"))
    native = native_components()
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
