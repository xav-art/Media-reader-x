"""Write THIRD_PARTY_NOTICES.txt: native-library summary + full Python package licenses.

Usage: python tools/gen_notices.py THIRD_PARTY_NOTICES.txt
"""

import json
import subprocess
import sys
from pathlib import Path

RUNTIME = ["PySide6-Essentials", "PySide6-Addons", "shiboken6", "pillow", "pillow-heif",
           "imagecodecs", "rawpy", "numpy", "psd-tools", "attrs"]

HEADER = """\
Media Reader x - third-party notices
==================================

Media Reader x's own source code is MIT licensed (see LICENSE.txt).
Source: https://github.com/xav-art/Media-reader-x

The binary distribution bundles the components below. Their licenses apply to
those components. Qt and FFmpeg are dynamically linked (separate DLLs in the
_internal folder) and may be replaced, as permitted by the LGPL.

Native libraries
----------------
- Qt 6 (QtCore, QtGui, QtWidgets, QtSvg, QtMultimedia) via PySide6
    LGPL-3.0. https://www.qt.io/licensing  Source: https://code.qt.io
- FFmpeg (shipped with Qt Multimedia, LGPL build: no GPL components)
    LGPL-2.1-or-later. https://ffmpeg.org/legal.html
- libheif (HEIC/HEIF decoding, via pillow-heif)
    LGPL-3.0. https://github.com/strukturag/libheif
- libde265 (HEVC image decoding, via pillow-heif)
    LGPL-3.0. https://github.com/strukturag/libde265
- x265 (HEVC encoder, statically included in the pillow-heif wheel; Media
    Reader only uses it in its --self-test). GPL-2.0-or-later.
    https://bitbucket.org/multicoreware/x265_git  Because this GPL component is
    part of the binary distribution, the distribution as a whole may be
    redistributed under the GPL-2.0-or-later; all corresponding source is public
    (this repository plus the upstream projects listed here).
- libjxl (JPEG XL, via imagecodecs)  BSD-3-Clause. https://github.com/libjxl/libjxl
- libavif, dav1d, libwebp, libjpeg-turbo, libpng, libtiff, zlib, lcms2, freetype
    (via Pillow)  BSD/zlib/IJG/MIT-style licenses. https://github.com/python-pillow/Pillow
- LibRaw (camera RAW, via rawpy)  LGPL-2.1 or CDDL-1.0. https://www.libraw.org
- CPython runtime  PSF-2.0. https://www.python.org

Python packages (full license texts)
------------------------------------
"""


def main(out: Path) -> None:
    raw = subprocess.run(
        [sys.executable, "-m", "piplicenses", "--format=json", "--with-license-file", "--no-license-path",
         "--with-urls", "--packages", *RUNTIME],
        check=True, capture_output=True, text=True,
    ).stdout
    parts = [HEADER]
    for pkg in sorted(json.loads(raw), key=lambda p: p["Name"].lower()):
        parts.append(f"\n{'=' * 78}\n{pkg['Name']} {pkg['Version']}  ({pkg['License']})\n{pkg['URL']}\n"
                     f"{'-' * 78}\n{pkg.get('LicenseText', '').strip()}\n")
    out.write_text("".join(parts), encoding="utf-8")
    print("notices written to", out)


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "THIRD_PARTY_NOTICES.txt"))
