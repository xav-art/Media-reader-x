# PyInstaller spec for Media Reader x (one-folder, windowed build).
# Run via build.ps1, which sets the working directory to the repo root.
# One-folder (not one-file) keeps start-up fast, avoids unpacking to %TEMP%
# (often blocked by AppLocker), and keeps the LGPL Qt/FFmpeg DLLs replaceable.

import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                 VarFileInfo, VarStruct, VSVersionInfo)

ROOT = Path(SPECPATH).parent
version = re.search(r'__version__ = "([^"]+)"', (ROOT / "src/mediareaderx/__init__.py").read_text()).group(1)
vtuple = tuple(int(p) for p in version.split(".")) + (0,)

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=vtuple, prodvers=vtuple),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "xav-art"),
            StringStruct("FileDescription", "Media Reader x - HEIC, AVIF, JXL, RAW and video viewer"),
            StringStruct("FileVersion", version),
            StringStruct("InternalName", "MediaReaderX"),
            StringStruct("LegalCopyright", "MIT License. See LICENSE and THIRD_PARTY_NOTICES.txt"),
            StringStruct("OriginalFilename", "MediaReaderX.exe"),
            StringStruct("ProductName", "Media Reader x"),
            StringStruct("ProductVersion", version),
        ])]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

a = Analysis(
    [str(ROOT / "packaging/launcher.py")],
    pathex=[str(ROOT / "src")],
    binaries=collect_dynamic_libs("pillow_heif") + collect_dynamic_libs("rawpy"),
    datas=[(str(ROOT / "src/mediareaderx/assets"), "mediareaderx/assets")],
    hiddenimports=[
        "pillow_heif", "rawpy",
        # imagecodecs loads codecs lazily; bundle only the JPEG XL one (~44 MB saved).
        "imagecodecs._jpegxl", "imagecodecs._shared", "imagecodecs._shared_cython",
    ],
    excludes=[
        "tkinter", "av", "pytest", "PyInstaller", "setuptools", "pip", "ssl", "numpy.random", "numpy.fft",
        "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        "PySide6.Qt3DCore", "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtPdf",
        "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtBluetooth",
    ],
    noarchive=False,
)
# Drop the unused imagecodecs codecs that the package hook would otherwise pull in.
_keep = ("_jpegxl", "_shared", "_imcd")
a.binaries = [b for b in a.binaries
              if not (b[0].startswith("imagecodecs") and b[0].endswith(".pyd") and not any(k in b[0] for k in _keep))]
# Never loaded in any session (1.3.1 review, measured): 45 MB of download, disk and antivirus scanning.
# - opengl32sw.dll: software OpenGL; the app draws with the raster engine and video with Direct3D 11.
# - the virtual keyboard plugin, and the Qt Quick/QML/OpenGL chain only it pulls in.
# - OpenSSL: the app makes no network calls (hashlib falls back to its built-in hashes).
# - Qt plugins for other backends: direct2d, TLS, Windows Media. Keep qoffscreen: --self-test runs on it
#   (dropping it made the frozen self-test abort with 0xC0000409).
_drop = ("opengl32sw.dll", "qtvirtualkeyboardplugin", "Qt6Quick", "Qt6Qml", "Qt6OpenGL.dll", "Qt6VirtualKeyboard",
         "libcrypto-3", "libssl-3", "_ssl.pyd", "_hashlib.pyd", "qdirect2d", "qschannelbackend",
         "qopensslbackend", "qcertonlybackend", "windowsmediaplugin")
a.binaries = [b for b in a.binaries if not any(d.lower() in b[0].replace("\\", "/").lower() for d in _drop)]

pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="MediaReaderX",
    icon=str(ROOT / "src/mediareaderx/assets/app.ico"),
    version=version_info,
    console=False,
    upx=False,  # UPX-packed binaries trigger antivirus false positives
)
coll = COLLECT(exe, a.binaries, a.datas, name="MediaReaderX", upx=False)
