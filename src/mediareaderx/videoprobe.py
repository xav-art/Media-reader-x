"""Which codecs does a video use? Read with FFmpeg's own demuxer, then check against a short list.

Qt plays video with its bundled FFmpeg, which has about 500 decoders. The container check in content.py
limits the demuxers; this module limits the codecs. It opens the file with the demuxer content.py picked,
through a read function (no path, no protocol: it runs inside a protected helper on a handle it was given),
runs FFmpeg's stream analysis (which decodes a few frames, inside the helper) and lists every stream.
The app plays a video only when every audio, video and subtitle stream uses a codec on the list.

The FFmpeg structures are read at fixed offsets that have not changed since FFmpeg 6 (AVFormatContext.pb,
nb_streams, streams; AVStream.codecpar; AVCodecParameters.codec_type, codec_id). The self-test checks them
on a sample, so a Qt update that changes them fails the build instead of the app.
"""

from __future__ import annotations

import ctypes
import functools
import logging
from pathlib import Path
from typing import BinaryIO

log = logging.getLogger(__name__)

MEDIA_TYPES = {0: "video", 1: "audio", 2: "data", 3: "subtitle", 4: "attachment"}
# Codecs the supported containers need (MP4/MOV, MKV/WebM, AVI, WMV, MPEG-TS, MPEG, FLV, Ogg, 3GP), and no
# more. Names as avcodec_get_name gives them.
ALLOWED = {
    "video": frozenset({"h264", "hevc", "vp8", "vp9", "av1", "mpeg1video", "mpeg2video", "mpeg4", "h263", "flv1",
                        "mjpeg", "png", "wmv1", "wmv2", "wmv3", "vc1", "theora"}),
    "audio": frozenset({"aac", "mp3", "mp2", "ac3", "eac3", "opus", "vorbis", "flac", "alac", "amr_nb", "wmav1", "wmav2",
                        "wmapro", "pcm_s16le", "pcm_s16be", "pcm_s24le", "pcm_s24be", "pcm_f32le", "pcm_u8",
                        "pcm_mulaw", "pcm_alaw"}),
    # FFmpeg's stream analysis opens a decoder for every subtitle stream (in the helper and again in the app's
    # player), even with subtitles off: text formats only. DVD, Blu-ray and DVB picture subtitles are refused.
    "subtitle": frozenset({"mov_text", "subrip", "webvtt", "ass", "text"}),
}
NEVER_DECODED = frozenset({"data", "attachment"})
MAX_STREAMS = 32

REFUSED = ("This video uses a format that Media Reader x does not play ({what}). "
           "Media Reader x plays only common video and sound formats, to keep your PC safe.")
DAMAGED = "The video is damaged or is not a supported video."

AVERROR_EOF = -0x20464F45  # FFERRTAG('E','O','F',' ')
AVSEEK_SIZE, AVSEEK_FORCE = 0x10000, 0x20000
_IO_BUFFER = 64 * 1024

_READ = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int)
_SEEK = ctypes.CFUNCTYPE(ctypes.c_int64, ctypes.c_void_p, ctypes.c_int64, ctypes.c_int)


class ProbeError(Exception):
    """FFmpeg could not read the file."""


@functools.cache
def _ffmpeg():
    """The avutil, avcodec and avformat DLLs that Qt Multimedia ships, with the functions used here."""
    import PySide6

    folder = Path(PySide6.__file__).parent

    def one(pattern: str) -> ctypes.CDLL:
        found = sorted(folder.glob(pattern))
        if len(found) != 1:
            raise OSError(f"expected one {pattern}, found {len(found)}")
        return ctypes.CDLL(str(found[0]))

    util, codec, fmt = one("avutil-*.dll"), one("avcodec-*.dll"), one("avformat-*.dll")

    def sig(lib, name, restype, *argtypes):
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = restype, list(argtypes)
        return fn

    vp = ctypes.c_void_p
    return {
        "malloc": sig(util, "av_malloc", vp, ctypes.c_size_t),
        "free": sig(util, "av_free", None, vp),
        "dict_set": sig(util, "av_dict_set", ctypes.c_int, ctypes.POINTER(vp), ctypes.c_char_p, ctypes.c_char_p,
                        ctypes.c_int),
        "dict_free": sig(util, "av_dict_free", None, ctypes.POINTER(vp)),
        "codec_name": sig(codec, "avcodec_get_name", ctypes.c_char_p, ctypes.c_int),
        "io_alloc": sig(fmt, "avio_alloc_context", vp, vp, ctypes.c_int, ctypes.c_int, vp, _READ, vp, _SEEK),
        "io_free": sig(fmt, "avio_context_free", None, ctypes.POINTER(vp)),
        "alloc": sig(fmt, "avformat_alloc_context", vp),
        "find_format": sig(fmt, "av_find_input_format", vp, ctypes.c_char_p),
        "open": sig(fmt, "avformat_open_input", ctypes.c_int, ctypes.POINTER(vp), ctypes.c_char_p, vp,
                    ctypes.POINTER(vp)),
        "find_info": sig(fmt, "avformat_find_stream_info", ctypes.c_int, vp, vp),
        "close": sig(fmt, "avformat_close_input", None, ctypes.POINTER(vp)),
    }


def _ptr(address: int, offset: int) -> int:
    return ctypes.c_void_p.from_address(address + offset).value or 0


def streams(f: BinaryIO, size: int, demuxer: str) -> list[tuple[str, str]]:
    """Every stream of the open video ``f`` as (media type, codec name). Raises ProbeError."""
    av = _ffmpeg()
    fmt = av["find_format"](demuxer.split(",")[0].encode("ascii"))
    if not fmt:
        raise ProbeError("demuxer missing")

    def read(_opaque, buf, n):
        try:
            data = f.read(n)
        except Exception:
            return AVERROR_EOF
        if not data:
            return AVERROR_EOF
        ctypes.memmove(buf, data, len(data))
        return len(data)

    def seek(_opaque, offset, whence):
        try:
            if whence & AVSEEK_SIZE:
                return size
            return f.seek(offset, whence & ~AVSEEK_FORCE)
        except Exception:
            return -1

    read_cb, seek_cb = _READ(read), _SEEK(seek)  # kept alive until the end of this call
    buffer = av["malloc"](_IO_BUFFER)
    io = av["io_alloc"](buffer, _IO_BUFFER, 0, None, read_cb, None, seek_cb)
    if not io:
        av["free"](buffer)
        raise MemoryError()
    ctx = ctypes.c_void_p(av["alloc"]())
    opts = ctypes.c_void_p()
    try:
        if not ctx.value:
            raise MemoryError()
        ctypes.c_void_p.from_address(ctx.value + 32).value = io  # AVFormatContext.pb: custom input
        for key, value in ((b"protocol_whitelist", b"none"), (b"max_streams", str(MAX_STREAMS).encode())):
            av["dict_set"](ctypes.byref(opts), key, value, 0)
        if av["open"](ctypes.byref(ctx), b"", fmt, ctypes.byref(opts)) < 0:
            ctx = ctypes.c_void_p()  # avformat_open_input frees the context on failure
            raise ProbeError("open failed")
        if av["find_info"](ctx, None) < 0:
            raise ProbeError("no stream information")
        count = ctypes.c_uint.from_address(ctx.value + 44).value
        if not 0 < count <= MAX_STREAMS:
            raise ProbeError("bad stream count")
        array = _ptr(ctx.value, 48)
        found = []
        for i in range(count):
            stream = _ptr(array, i * 8)
            params = _ptr(stream, 16) if stream else 0
            if not params:
                raise ProbeError("bad stream")
            kind = ctypes.c_int.from_address(params).value
            codec_id = ctypes.c_int.from_address(params + 4).value
            name = av["codec_name"](codec_id) or b"unknown"
            found.append((MEDIA_TYPES.get(kind, "unknown"), name.decode("ascii", "replace")))
        return found
    finally:
        av["dict_free"](ctypes.byref(opts))
        if ctx.value:
            av["close"](ctypes.byref(ctx))
        # The demuxer may have replaced the buffer: free the context's current one.
        io_ptr = ctypes.c_void_p(io)
        current = _ptr(io, 8)  # AVIOContext.buffer (after av_class)
        if current:
            av["free"](current)
        av["io_free"](ctypes.byref(io_ptr))


def refusal(found: list[tuple[str, str]]) -> str | None:
    """None when every decoded stream uses an allowed codec, else the user-facing reason."""
    if not found or not any(kind == "video" for kind, _name in found):
        return DAMAGED
    if len(found) > MAX_STREAMS:
        return DAMAGED
    bad = sorted({name for kind, name in found
                  if kind not in NEVER_DECODED and name not in ALLOWED.get(kind, frozenset())})
    if bad:
        what = ", ".join(n[:24] for n in bad[:3])
        return REFUSED.format(what=what)
    return None
