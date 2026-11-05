"""Write THIRD_PARTY_NOTICES.txt for the Media Reader x binary distribution.

Parts of the file:
1. Licences in short: what applies to what, replacing the LGPL libraries, where the source is, patents.
2. The native libraries. Versions are read from the build environment by sbom_native.py, which also holds
   each library's SPDX licence, copyright line and version-pinned source URL (the SBOM uses the same data).
3. Bundled parts that are not in the SBOM: Qt for Python, the PyInstaller bootloader, winpthreads and the
   Microsoft C runtime.
4. Full licence texts: packaging/licenses, the licences of the libraries inside imagecodecs' JPEG XL
   module, and CPython's licence.
5. The Python packages' own licence files (every License-File in their metadata).

The script fails when packaging/sources.json no longer matches a bundled version, so the source archives
that build.ps1 copies to dist\\source always match the binaries.

Usage: python tools/gen_notices.py THIRD_PARTY_NOTICES.txt
"""

from __future__ import annotations

import importlib.metadata
import json
import re
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sbom_native  # noqa: E402  (same folder)

ROOT = Path(__file__).resolve().parents[1]
LICENSES = ROOT / "packaging" / "licenses"
SOURCES = ROOT / "packaging" / "sources.json"
WIDTH = 100

# Python packages whose code ships in the bundle. charset-normalizer comes in through numpy.f2py.
RUNTIME = ["PySide6-Essentials", "PySide6-Addons", "shiboken6", "pillow", "pillow-heif", "imagecodecs", "rawpy",
           "numpy", "psd-tools", "attrs", "charset-normalizer", "pyinstaller"]

# Licence texts from packaging/licenses, in print order: (file, title, what it applies to).
LICENSE_FILES = [
    ("GPL-3.0.txt", "GNU General Public License, version 3",
     "the binary distribution as a whole; the GCC runtime; the base of the LGPL-3.0"),
    ("GPL-2.0.txt", "GNU General Public License, version 2", "x265; FreeType's GPL option"),
    ("LGPL-3.0.txt", "GNU Lesser General Public License, version 3",
     "Qt, PySide6, shiboken6, libheif, libde265"),
    ("LGPL-2.1.txt", "GNU Lesser General Public License, version 2.1", "FFmpeg, LibRaw"),
    ("GCC-exception-3.1.txt", "GCC Runtime Library Exception, version 3.1",
     "libgcc_s_seh-1.dll, libstdc++-6.dll"),
    ("libaom-LICENSE.txt", "libaom licence (BSD-2-Clause)", "libaom, in Pillow's AVIF module"),
    ("libaom-PATENTS.txt", "Alliance for Open Media Patent License 1.0", "libaom"),
    ("dav1d-COPYING.txt", "dav1d licence (BSD-2-Clause)", "dav1d, in Pillow's AVIF module"),
    ("libjxl-LICENSE.txt", "libjxl licence (BSD-3-Clause)", "libjxl, in imagecodecs' JPEG XL module"),
    ("libjxl-PATENTS.txt", "libjxl additional IP rights grant (patents)", "libjxl"),
    ("winpthreads-COPYING.txt", "MinGW-w64 winpthreads licence (MIT and BSD-3-Clause)", "libwinpthread-1.dll"),
]

# Licence files from the installed imagecodecs wheel for the libraries inside its JPEG XL module.
IMAGECODECS_LICENSES = [
    ("LICENSE-highway", "Highway licence (Apache-2.0; also offered under BSD-3-Clause)"),
    ("LICENSE-brotli", "Brotli licence (MIT)"),
    ("LICENSE-lcms2", "Little CMS licence (MIT)"),
]

# The FreeType licence asks for a credit with the release year of the FreeType version used.
FREETYPE_YEARS = {"2.14.3": "2026"}


def _wrap(text: str, indent: str = "") -> str:
    return textwrap.fill(text, WIDTH, initial_indent=indent, subsequent_indent=indent,
                         break_long_words=False, break_on_hyphens=False)


def _bullets(items: list[str]) -> str:
    return "\n".join(_wrap(item, "  ").replace("  ", "- ", 1) for item in items) + "\n"


def _heading(title: str, char: str = "-") -> str:
    return f"\n{title}\n{char * len(title)}\n"


def _app_version() -> str:
    init = (ROOT / "src" / "mediareaderx" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__ = "([^"]+)"', init).group(1)


def _license_id(comp: dict) -> str:
    entry = comp["licenses"][0]
    return entry.get("expression") or entry["license"]["id"]


def _property(comp: dict, key: str) -> str | None:
    for prop in comp.get("properties", []):
        if prop["name"] == f"mediareaderx:{key}":
            return prop["value"]
    return None


def _check_sources(native: list[dict], sources: list[dict]) -> None:
    """Fail when a shipped source archive is for another version than the binary that ships."""
    versions = {c["name"]: c["version"] for c in native}
    for entry in sources:
        if "component" in entry:
            actual = versions.get(entry["component"])
        else:
            actual = importlib.metadata.version(entry["package"])
        if actual != entry["version"]:
            raise SystemExit(
                f"packaging/sources.json: {entry['file']} is for {entry.get('component') or entry['package']} "
                f"{entry['version']}, the build has {actual}. Update the entry, empty its sha256 and run "
                "tools/fetch_sources.py --pin.")


def _summary(version: str, sources: list[dict], freetype_year: str) -> str:
    shipped = ", ".join(dict.fromkeys(e.get("component") or e["package"] for e in sources))
    out = _heading("1. Licences in short")
    out += _bullets([
        f"Media Reader x's own source code is licensed under the MIT licence (LICENSE.txt). "
        f"Source: https://github.com/xav-art/Media-reader-x (tag v{version}).",
        "The binary distribution (Setup.exe, the portable zip and the installed app) also contains the "
        "third-party components in parts 2 and 3. Each component keeps its own licence.",
        "One of them, x265, is licensed under GPL-2.0-or-later. It ships together with LGPL-3.0 libraries "
        "(Qt, libheif, libde265). The licence that covers all of them together is the GNU General Public "
        "License version 3. So the binary distribution, taken as a whole, is conveyed under GPL-3.0. This "
        "does not change the licence of any part: Media Reader x's own code stays MIT, and you may use each "
        "component under its own licence.",
        "Media Reader x never calls the x265 encoder. The libheif DLL imports libx265 because pillow-heif "
        "builds libheif with HEIC encoding. Media Reader x removes Pillow's HEIF writer at startup, so no "
        "code path reaches x265.",
    ])
    out += _heading("Replacing the LGPL libraries")
    out += _bullets([
        "Qt and Qt for Python (Qt6*.dll and the .pyd files in _internal\\PySide6), FFmpeg (avcodec, avformat, "
        "avutil, swresample and swscale DLLs in _internal\\PySide6), libheif and libde265 (libheif-*.dll and "
        "libde265-*.dll in _internal) and LibRaw (raw_r.dll in _internal\\rawpy) are separate DLLs that load "
        "at run time. You may replace any of them with your own build of a compatible version. Keep the "
        "file name.",
        "Nothing in Media Reader x's licence stops you from changing these libraries, or from reverse "
        "engineering Media Reader x to debug such changes.",
    ])
    out += _heading("Source code")
    out += _bullets([
        f"The source archives of the GPL and LGPL components ({shipped}), including the MSYS2 build recipes "
        "for x265 and libde265, are in the \"source\" folder next to Setup.exe and the portable zip. "
        "SHA256SUMS.txt lists their checksums, and source\\SOURCES.txt says what each file is.",
        "Every other component has a version-pinned source link in parts 2 and 3.",
        "If your copy came without the source folder, use those links, or ask the person who gave you "
        "Media Reader x.",
    ])
    out += _heading("Patents")
    out += _bullets([
        "Media Reader x and its author grant no patent licence. Media formats such as HEVC (H.265), AV1, VP9 "
        "and others can be covered by patents in some countries. You are responsible for any patent licence "
        "that you or your organisation need where you use Media Reader x.",
        "Some components come with their own patent grant (the Alliance for Open Media Patent License 1.0 "
        "for libaom, and Google's patent grant for libjxl, both in part 4). These grants apply as their "
        "texts say.",
    ])
    out += _heading("Warranty")
    out += _bullets([
        "Media Reader x is provided \"as is\", without warranty of any kind (see LICENSE.txt). The third-party "
        "components carry the warranty disclaimers of their own licences.",
    ])
    out += _heading("Required acknowledgements")
    out += _bullets([
        "This software is based in part on the work of the Independent JPEG Group.",
        f"Portions of this software are copyright (C) {freetype_year} The FreeType Project "
        "(www.freetype.org). All rights reserved.",
    ])
    return out


def _native_table(native: list[dict], sources: list[dict]) -> str:
    out = _heading("2. Native libraries")
    out += _wrap("Versions are read from the build environment. \"Ships in\" names the Python wheel that "
                 "contains the library.") + "\n"
    shipped: dict[str, list[str]] = {}
    for entry in sources:
        if "component" in entry:
            shipped.setdefault(entry["component"], []).append("source\\" + entry["file"].replace("/", "\\"))
    for comp in native:
        out += f"\n{comp['name']} {comp['version']}    {_license_id(comp)}\n"
        out += _wrap(comp["copyright"], "    ") + "\n"
        out += _wrap(f"Used for: {comp['description']}", "    ") + "\n"
        out += f"    Ships in: {comp['_wheel'] or 'the Python runtime'}\n"
        ref = comp["externalReferences"][0]
        note = f" ({ref['comment']})" if ref.get("comment") else ""
        out += f"    Source: {ref['url']}{note}\n"
        if comp["name"] in shipped and comp["bom-ref"] == f"native:{comp['name']}":
            out += _wrap("Source in this distribution: " + ", ".join(shipped[comp["name"]]), "    ") + "\n"
        for key in ("build", "configure", "also-contains"):
            value = _property(comp, key)
            if value:
                out += _wrap(f"{key.capitalize()}: {value}", "    ") + "\n"
    return out


def _others() -> str:
    pyside = importlib.metadata.version("PySide6-Essentials")
    pyinstaller = importlib.metadata.version("pyinstaller")
    entries = [
        (f"Qt for Python (PySide6, shiboken6) {pyside}", "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only",
         "Copyright (C) The Qt Company Ltd.",
         "Python bindings for Qt. The wheels' own licence file holds only Qt's commercial terms; Media Reader x "
         "uses Qt for Python under the LGPL-3.0 (part 4).",
         f"https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-{pyside}-src/"
         f"pyside-setup-everywhere-src-{pyside}.tar.xz (git tag v{pyside})"),
        (f"PyInstaller {pyinstaller} bootloader and run-time hooks", "GPL-2.0-or-later WITH Bootloader-exception; "
         "hooks Apache-2.0", "Copyright (c) 2010 and later, PyInstaller Development Team",
         "MediaReaderX.exe starts the app with PyInstaller's bootloader. The bootloader exception allows it in "
         "any program; the full terms are under \"pyinstaller\" in part 5.",
         f"https://github.com/pyinstaller/pyinstaller/archive/refs/tags/v{pyinstaller}.tar.gz"),
        ("MinGW-w64 winpthreads (libwinpthread-1.dll)", "MIT AND BSD-3-Clause",
         "Copyright (c) 2011 mingw-w64 project; (C) 2010 Lockless Inc.",
         "Thread library of the MSYS2-built HEIF DLLs (pillow-heif wheel).",
         "https://github.com/mingw-w64/mingw-w64 (mingw-w64-libraries/winpthreads), built by MSYS2: "
         "https://github.com/msys2/MINGW-packages/tree/master/mingw-w64-winpthreads"),
        ("Microsoft Visual C++ runtime", "Microsoft Distributable Code",
         "Copyright (c) Microsoft Corporation",
         "vcruntime140*.dll, msvcp140*.dll, vcomp140.dll, ucrtbase.dll and api-ms-win-*.dll, redistributed "
         "under the Visual Studio licence terms (see the CPython licence in part 4).",
         "not applicable (no source is published)"),
    ]
    out = _heading("3. Other bundled components")
    for name, lic, copyright_line, use, source in entries:
        out += f"\n{name}    {lic}\n{_wrap(copyright_line, '    ')}\n{_wrap(use, '    ')}\n"
        out += _wrap(f"Source: {source}", "    ") + "\n"
    return out


def _text_block(title: str, applies: str | None, text: str) -> str:
    rule = "=" * WIDTH
    head = f"\n{rule}\n{title}\n"
    if applies:
        head += _wrap(f"Applies to: {applies}") + "\n"
    return f"{head}{'-' * WIDTH}\n{text.strip(chr(13) + chr(10)).rstrip()}\n"  # keep the first line's indent


def _license_texts() -> str:
    import imagecodecs

    out = _heading("4. Licence texts")
    for name, title, applies in LICENSE_FILES:
        out += _text_block(title, applies, (LICENSES / name).read_text(encoding="utf-8"))
    folder = Path(imagecodecs.__file__).parent / "licenses"
    for name, title in IMAGECODECS_LICENSES:
        out += _text_block(title, "imagecodecs' JPEG XL module (libjxl uses it)",
                           (folder / name).read_text(encoding="utf-8"))
    cpython = Path(sys.base_prefix) / "LICENSE.txt"
    out += _text_block("CPython licence (with the licences of the libraries in its Windows build)",
                       "python314.dll and the Python standard library", cpython.read_text(encoding="utf-8"))
    return out


def _license_files(dist: importlib.metadata.Distribution) -> list[tuple[str, str]]:
    """Every licence file a package declares (License-File), or its dist-info licence files if it declares none."""
    files = [f for f in (dist.files or []) if ".dist-info/" in f.as_posix()]
    declared = dist.metadata.get_all("License-File") or []
    if declared:
        chosen = []
        for name in declared:
            hits = ([f for f in files if f.as_posix().endswith(f"/licenses/{name}")]
                    or [f for f in files if f.as_posix().endswith(f"/{name}")])
            if not hits:
                raise SystemExit(f"{dist.metadata['Name']}: licence file {name} is not installed")
            chosen.append(hits[0])
    else:
        chosen = [f for f in files if re.search(r"(LICEN[CS]E|COPYING|NOTICE)", f.name, re.I)]
    if not chosen:
        raise SystemExit(f"{dist.metadata['Name']} ships no licence file")
    return [(f.as_posix().split("/licenses/")[-1], f.read_text(encoding="utf-8")) for f in chosen]


def _python_packages() -> str:
    out = _heading("5. Python packages (their own licence files)")
    for name in sorted(RUNTIME, key=str.lower):
        dist = importlib.metadata.distribution(name)
        meta = dist.metadata
        lic = meta.get("License-Expression") or meta.get("License") or "see the licence file"
        urls = [u.split(",", 1)[-1].strip() for u in meta.get_all("Project-URL") or []]
        url = meta.get("Home-page") or (urls[0] if urls else f"https://pypi.org/project/{name}/")
        for index, (file_name, text) in enumerate(_license_files(dist)):
            if index == 0:
                title = f"{meta['Name']} {dist.version}\nLicence: {lic}\n{url}"
            else:
                title = f"{meta['Name']} {dist.version}: {file_name}"
            out += _text_block(title, None, text)
    return out


def main(out: Path) -> None:
    version = _app_version()
    native = sbom_native.native_components()
    sources = json.loads(SOURCES.read_text(encoding="utf-8"))["sources"]
    _check_sources(native, sources)
    freetype = {c["version"] for c in native if c["name"] == "freetype"}
    unknown = freetype - FREETYPE_YEARS.keys()
    if unknown:
        raise SystemExit(f"add the release year of FreeType {sorted(unknown)} to FREETYPE_YEARS in gen_notices.py")
    year = max(FREETYPE_YEARS[v] for v in freetype)
    title = f"Media Reader x {version}: third-party notices"
    parts = [f"{title}\n{'=' * len(title)}\n", _summary(version, sources, year), _native_table(native, sources),
             _others(), _license_texts(), _python_packages()]
    out.write_text("".join(parts), encoding="utf-8")
    print("notices written to", out)


if __name__ == "__main__":
    try:
        main(Path(sys.argv[1] if len(sys.argv) > 1 else "THIRD_PARTY_NOTICES.txt"))
    except sbom_native.VersionError as exc:
        print(f"Notices error: {exc}", file=sys.stderr)
        sys.exit(1)
