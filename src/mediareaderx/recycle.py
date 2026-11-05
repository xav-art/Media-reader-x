"""Can Windows put this file in the Recycle Bin, or would "delete" destroy it?

Qt's ``QFile.moveToTrash`` asks Windows to recycle the file without any prompt. Where Windows cannot
recycle (network folders, USB sticks, memory cards, a drive set to "Don't move files to the Recycle Bin",
a file larger than the Recycle Bin), it then deletes the file permanently. Media Reader x never deletes
permanently, so it checks first and refuses on those drives.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes as wt
from pathlib import Path

log = logging.getLogger(__name__)

DRIVE_FIXED = 3
_BITBUCKET = r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume"
_POLICY = r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer"

NO_RECYCLE_BIN = ("Media Reader x did not delete \"{name}\". This drive has no Recycle Bin (for example a network "
                  "folder, a USB drive or a memory card), and Media Reader x never deletes files permanently. "
                  "To delete it, use File Explorer.")
TOO_BIG = ("Media Reader x did not delete \"{name}\". The file is larger than the Recycle Bin on this drive, so "
           "Windows would delete it permanently, and Media Reader x never does that. To delete it, use File Explorer.")


def _volume_root(path: Path) -> str | None:
    k32 = ctypes.windll.kernel32
    buf = ctypes.create_unicode_buffer(512)
    if not k32.GetVolumePathNameW(str(path), buf, len(buf)):
        return None
    return buf.value


def _volume_guid(root: str) -> str | None:
    buf = ctypes.create_unicode_buffer(64)
    if not ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(root, buf, len(buf)):
        return None
    value = buf.value  # \\?\Volume{GUID}\
    start, end = value.find("{"), value.find("}")
    return value[start:end + 1] if 0 <= start < end else None


def _dword(key: str, name: str) -> int | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            value, kind = winreg.QueryValueEx(k, name)
    except OSError:
        return None
    return value if kind == winreg.REG_DWORD else None


def refusal(path: Path) -> str | None:
    """None when Windows will put ``path`` in the Recycle Bin, else the message that says why it will not."""
    if sys.platform != "win32":
        return None
    name = path.name
    text = str(path)
    if text.startswith(("\\\\", "//")):  # UNC path, including \\?\UNC\ and \\?\ long paths to shares
        if not text.startswith("\\\\?\\") or text.upper().startswith("\\\\?\\UNC\\"):
            return NO_RECYCLE_BIN.format(name=name)
    root = _volume_root(path)
    if root is None or ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)) != DRIVE_FIXED:
        return NO_RECYCLE_BIN.format(name=name)
    if _dword(_POLICY, "NoRecycleFiles") == 1:
        return NO_RECYCLE_BIN.format(name=name)
    guid = _volume_guid(root)
    if guid is not None:
        key = f"{_BITBUCKET}\\{guid}"
        if _dword(key, "NukeOnDelete") == 1:
            return NO_RECYCLE_BIN.format(name=name)
        capacity_mb = _dword(key, "MaxCapacity")
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if capacity_mb is not None and size > capacity_mb * 1024 * 1024:
            return TOO_BIG.format(name=name)
    return None
