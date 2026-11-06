"""The protected helper: decodes what the app hands it, inside an AppContainer (see docs/SANDBOX.md).

Started as ``MediaReaderX.exe --sandbox-helper <in> <out>`` (two inherited pipe handles). It receives
requests with a read-only file handle and the file's extension, never a path, and replies with pixels and
plain values. It cannot read other files, write outside its own folder, use the network or start programs.
"""

from __future__ import annotations

import logging
import msvcrt
import os
import sys
from typing import BinaryIO

from mediareaderx.sandbox.protocol import HelperGone, image_parts, read_header, write_message

THUMB_SIDE = 192
_HOLD: list = []  # pictures whose memory a reply's views point into, until the reply is written
VIDEO_TIMEOUT_MS = 8000
MAX_NOTES = 20


class _Notes(logging.Handler):
    """Warnings from the decoders, returned with the reply (the helper cannot write the app's log)."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.items: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if len(self.items) < MAX_NOTES:
            try:
                self.items.append(record.getMessage()[:200])
            except Exception:
                pass


def _open(handle: int) -> BinaryIO:
    return os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY), "rb")


def _size(f: BinaryIO) -> int:
    return os.fstat(f.fileno()).st_size


def _decode(req: dict, f: BinaryIO) -> tuple[dict, list]:
    from mediareaderx import decoders

    size = _size(f)
    decoders.check_file_size(size)
    data = f.read(size)
    image = decoders.decode_bytes(data, req["ext"], req.get("max_side"))
    del data
    frames, blobs = [], []
    for frame in image.frames:
        spec, pixels = image_parts(frame, _HOLD)
        frames.append(spec)
        blobs.append(pixels)
    exif = image.exif_bytes or b""
    if exif:
        blobs.append(exif)
    full = image.full_size
    return {"ok": True, "frames": frames, "durations": image.durations_ms, "format_name": image.format_name,
            "metadata": [list(m) for m in image.metadata], "full_size": [full.width(), full.height()] if full else None,
            "notice": image.notice, "exif": len(exif)}, blobs


def _thumb(req: dict, f: BinaryIO) -> tuple[dict, list]:
    from mediareaderx import thumbnails

    img = thumbnails.thumbnail_from(f, _size(f), req["ext"], req.get("side") or THUMB_SIDE)
    if img is None or img.isNull():
        return {"ok": True, "frames": []}, []
    spec, pixels = image_parts(img, _HOLD)
    return {"ok": True, "frames": [spec]}, [pixels]


def _grab_frame(f: BinaryIO, ext: str, side: int, skip_frame: bool):
    """One frame (scaled to ``side``) and the length in ms, with Qt's own player reading the handle."""
    from PySide6.QtCore import QEventLoop, QFile, QFileDevice, QIODevice, QSize, Qt, QTimer, QUrl
    from PySide6.QtMultimedia import QMediaPlayer, QVideoSink

    f.seek(0)
    qfile = QFile()
    if not qfile.open(f.fileno(), QIODevice.OpenModeFlag.ReadOnly, QFileDevice.FileHandleFlag.DontCloseHandle):
        return None, 0
    player, sink, loop = QMediaPlayer(), QVideoSink(), QEventLoop()
    player.setVideoSink(sink)
    state: dict = {"img": None, "duration": 0}
    status = QMediaPlayer.MediaStatus

    def on_status(st) -> None:
        if st == status.LoadedMedia:
            state["duration"] = max(0, player.duration())
            if skip_frame:
                loop.quit()
                return
            player.setPosition(min(1000, player.duration() // 10))  # past black fade-in frames
            player.play()
        elif st in (status.InvalidMedia, status.EndOfMedia):
            loop.quit()

    def on_frame(frame) -> None:
        if frame.isValid() and state["img"] is None:
            img = frame.toImage()
            if not img.isNull():
                state["img"] = img.scaled(QSize(side, side), Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation)
                loop.quit()

    player.mediaStatusChanged.connect(on_status)
    sink.videoFrameChanged.connect(on_frame)
    player.errorOccurred.connect(lambda *_a: loop.quit())
    timer = QTimer()  # its own timer, stopped below: a pending one would cut the next video's grab short
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(VIDEO_TIMEOUT_MS)
    player.setSourceDevice(qfile, QUrl.fromLocalFile("video" + ext))
    if player.mediaStatus() not in (status.InvalidMedia,):
        loop.exec()
    timer.stop()
    player.stop()
    player.setSource(QUrl())
    qfile.close()
    return state["img"], state["duration"]


def _video(req: dict, f: BinaryIO) -> tuple[dict, list]:
    from mediareaderx import content, videoprobe

    ext, size = req["ext"], _size(f)
    head = f.read(content.HEAD_BYTES)
    try:
        notice = content.check_video_head(head, ext, size)
        demuxer = content.ffmpeg_demuxer(head, ext.lower(), at_end=size <= len(head))
    except content.Refused as exc:
        return {"ok": True, "refused": str(exc), "streams": []}, []
    f.seek(0)
    try:
        # Playback check: also read on, so streams that appear later in the file are listed too.
        scan = 0 if req.get("frame") else videoprobe.scan_bytes_for(demuxer or "")
        found = videoprobe.streams(f, size, demuxer or "", scan_bytes=scan)
    except videoprobe.ProbeError:
        return {"ok": True, "refused": videoprobe.DAMAGED, "streams": []}, []
    reply = {"ok": True, "notice": notice, "streams": [list(s) for s in found], "frames": [], "duration": 0}
    if not req.get("frame") or videoprobe.refusal(found) is not None:
        return reply, []
    # AV1 decodes only on a graphics card, and helpers do not use it: read the length only.
    av1 = any(name == "av1" for kind, name in found if kind == "video")
    img, reply["duration"] = _grab_frame(f, ext, req.get("side") or THUMB_SIDE, skip_frame=av1)
    if img is None:
        return reply, []
    spec, pixels = image_parts(img, _HOLD)
    reply["frames"] = [spec]
    return reply, [pixels]


def _selfcheck(req: dict) -> tuple[dict, list]:
    """For --self-test only: try what an attacker would try, and report what was blocked."""
    results = {}
    try:
        with open(req["path"], "rb") as probe:
            probe.read(1)
        results["read"] = "open"
    except OSError:
        results["read"] = "blocked"
    try:
        import socket

        # TEST-NET-1 (RFC 5737): never routed, so a sandbox that failed sends nothing anywhere real.
        socket.create_connection(("192.0.2.1", 9), timeout=2).close()
        results["network"] = "open"
    except PermissionError:
        results["network"] = "blocked"
    except OSError as exc:  # refused or unreachable: the socket itself was allowed
        results["network"] = "blocked" if getattr(exc, "winerror", None) == 10013 else "open"
    try:
        import subprocess

        subprocess.run(["cmd", "/c", "exit"], capture_output=True, timeout=5)
        results["process"] = "open"
    except OSError:
        results["process"] = "blocked"
    results["screen"] = _screen_capture()
    results["user_desktop"] = _user_desktop()
    results["station"] = _own_station()
    return {"ok": True, "results": results}, []


def _user32():
    import ctypes
    from ctypes import wintypes as wt

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.OpenWindowStationW.restype = wt.HANDLE
    user32.OpenWindowStationW.argtypes = [wt.LPCWSTR, wt.BOOL, wt.DWORD]
    user32.OpenDesktopW.restype = wt.HANDLE
    user32.OpenDesktopW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.BOOL, wt.DWORD]
    user32.GetProcessWindowStation.restype = wt.HANDLE
    user32.GetUserObjectInformationW.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD,
                                                 ctypes.POINTER(wt.DWORD)]
    return user32


def _user_desktop() -> str:
    """"blocked" when the helper cannot open the user's window station or desktop (WinSta0\\Default) because
    access is denied. Any other failure (user32 not working, for example) is reported as an error, so the
    self-test never counts a broken helper as a contained one."""
    import ctypes
    import threading
    from ctypes import wintypes as wt

    user32 = _user32()
    found, errors = [], []

    def attempt() -> None:  # a new thread, as an attacker would use: its desktop is not yet fixed
        station = user32.OpenWindowStationW("WinSta0", False, 0x1 | 0x2 | 0x100)
        errors.append(0 if station else ctypes.get_last_error())
        desktop = user32.OpenDesktopW("Default", 0, False, 0x1 | 0x40 | 0x80)
        errors.append(0 if desktop else ctypes.get_last_error())
        found.extend(h for h in (station, desktop) if h)
        if station:
            user32.CloseWindowStation(wt.HANDLE(station))
        if desktop:
            user32.CloseDesktop(wt.HANDLE(desktop))

    thread = threading.Thread(target=attempt)
    thread.start()
    thread.join()
    if found:
        return "open"
    # WinSta0: 5 (ERROR_ACCESS_DENIED). "Default" is looked up in the helper's own station: 2 (not found) or 5.
    return "blocked" if errors[0] == 5 and errors[1] in (2, 5) else f"error {errors}"


def _own_station() -> str:
    """"separate" when the helper's own window station is not the user's (WinSta0)."""
    import ctypes

    user32 = _user32()
    name = ctypes.create_unicode_buffer(256)
    if not user32.GetUserObjectInformationW(user32.GetProcessWindowStation(), 2, name, ctypes.sizeof(name), None):
        return f"error {ctypes.get_last_error()}"
    return "WinSta0" if name.value.lower() == "winsta0" else "separate"


def _screen_capture() -> str:
    """"blocked" when a capture of the screen shows nothing: the helper runs on its own empty desktop."""
    import ctypes

    user32, gdi32 = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32")
    for fn in (user32.GetDC, gdi32.CreateCompatibleDC, gdi32.CreateCompatibleBitmap, gdi32.SelectObject):
        fn.restype = ctypes.c_void_p
    user32.GetDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    gdi32.GetBitmapBits.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p]
    side = 200
    screen = user32.GetDC(None)
    if not screen:
        return "blocked"
    mem = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, side, side)
    try:
        if not mem or not bmp:
            return "blocked"
        gdi32.SelectObject(mem, bmp)
        if not gdi32.BitBlt(mem, 0, 0, side, side, screen, 0, 0, 0x00CC0020):  # SRCCOPY
            return "blocked"
        buf = ctypes.create_string_buffer(side * side * 4)
        gdi32.GetBitmapBits(ctypes.c_void_p(bmp), len(buf), buf)
        colours = {buf.raw[i:i + 3] for i in range(0, len(buf), 4)}
        return "blocked" if len(colours) <= 1 else "open"
    finally:
        if bmp:
            gdi32.DeleteObject(ctypes.c_void_p(bmp))
        if mem:
            gdi32.DeleteDC(ctypes.c_void_p(mem))
        user32.ReleaseDC(None, ctypes.c_void_p(screen))


_app = None


def _qt_app() -> None:
    """Qt's application object, made on first need (a video check without a frame does not need it)."""
    global _app
    if _app is None:
        from PySide6.QtGui import QGuiApplication

        _app = QGuiApplication.instance() or QGuiApplication(["MediaReaderX-helper"])


class _NoDecodeError(Exception):
    pass


def _handle(req: dict, notes: _Notes) -> tuple[dict, list]:
    op = req.get("op")
    if op in ("decode", "thumb") or (op == "video" and req.get("frame")):
        _qt_app()
        from mediareaderx.decoders import DecodeError
    else:
        DecodeError = _NoDecodeError  # noqa: N806 (the video check never imports the image decoders)
    notes.items.clear()
    if op == "ping":
        return {"ok": True}, []
    if op == "selfcheck":
        return _selfcheck(req)
    handle = req.get("h")
    if op not in ("decode", "thumb", "video") or type(handle) is not int or not isinstance(req.get("ext"), str):
        return {"ok": False, "error": "bad request"}, []
    try:
        with _open(handle) as f:
            reply, blobs = {"decode": _decode, "thumb": _thumb, "video": _video}[op](req, f)
    except DecodeError as exc:
        reply, blobs = {"ok": False, "error": str(exc)}, []
    except MemoryError:
        reply, blobs = {"ok": False, "error": "Not enough memory to open this file.", "memory": True}, []
    except Exception as exc:  # never let a bad file end the loop without a reply
        logging.getLogger(__name__).warning("Helper %s failed: %s", op, type(exc).__name__)
        reply, blobs = {"ok": False, "error": "The file is damaged or cannot be read."}, []
    if notes.items:
        reply["notes"] = list(notes.items)
    return reply, blobs


def main(argv: list[str]) -> int:
    try:
        return _main(argv)
    except BaseException:
        import traceback

        try:
            with open(os.path.join(os.environ.get("TEMP", "."), "helper-error.txt"), "w") as f:
                traceback.print_exc(file=f)
        except OSError:
            pass
        return 1


def _main(argv: list[str]) -> int:
    in_handle, out_handle = int(argv[0]), int(argv[1])
    fin = os.fdopen(msvcrt.open_osfhandle(in_handle, os.O_RDONLY | os.O_BINARY), "rb", buffering=0)
    fout = os.fdopen(msvcrt.open_osfhandle(out_handle, os.O_WRONLY | os.O_BINARY), "wb", buffering=0)
    # A native crash leaves its stack in the helper's own temp folder (the only place it can write),
    # where a developer can read it; the folder is emptied when the slot is used again.
    import faulthandler

    try:
        crash_log = open(os.path.join(os.environ.get("TEMP", "."), "helper-crash.txt"), "w")  # noqa: SIM115
        faulthandler.enable(crash_log)
    except OSError:
        pass
    notes = _Notes()
    root = logging.getLogger()
    root.handlers[:] = [notes]
    root.setLevel(logging.WARNING)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"

    from mediareaderx import __version__

    if os.environ.get("MRX_HELPER_ROLE") != "check":  # load the codecs now, so the first file is fast
        _qt_app()
        from mediareaderx import decoders

        decoders.warm_up()
    write_message(fout, {"op": "ready", "version": __version__, "pid": os.getpid()})
    while True:
        try:
            req = read_header(fin)
        except HelperGone:
            return 0
        reply, blobs = _handle(req, notes)
        try:
            write_message(fout, reply, blobs)
        finally:
            _HOLD.clear()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
