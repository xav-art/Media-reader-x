"""Protected decoding (1.6.0): helpers, the reply checks, the fail-closed rule and what a helper cannot do."""

from __future__ import annotations

import io
import json
import struct
import sys
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtGui import QImage

from mediareaderx import content, sandbox, videoprobe
from mediareaderx.decoders import DecodeError
from mediareaderx.sandbox import protocol

windows = pytest.mark.skipif(sys.platform != "win32", reason="AppContainers exist only on Windows")


def _message(header: dict, blobs: list[bytes] = ()) -> io.BytesIO:
    out = io.BytesIO()
    protocol.write_message(out, header, blobs)
    out.seek(0)
    return out


def _reply(header: dict, blobs: list[bytes] = ()) -> tuple[dict, io.BytesIO]:
    f = _message(header, blobs)
    return protocol.read_header(f), f


def _good(**over) -> tuple[dict, list[bytes]]:
    header = {"ok": True, "frames": [[2, 1, "rgb32"]], "durations": [0], "format_name": "PNG",
              "metadata": [["Image", "Format", "PNG"]], "full_size": None, "notice": None, "exif": 0}
    header.update(over)
    return header, [bytes(8)]


# -- reply checks (a hostile helper) ------------------------------------------------------------------
def test_a_good_reply_becomes_an_image(qapp):
    header, f = _reply(*_good())
    image = sandbox._read_decoded(header, f)
    assert image.frames[0].size().width() == 2 and image.format_name == "PNG"


@pytest.mark.parametrize("over", [
    {"frames": [[2, 1, "rgb888"]]},  # a format outside the two allowed
    {"frames": [[0, 1, "rgb32"]]},
    {"frames": [[70000, 1, "rgb32"]]},
    {"frames": [[3, 1, "rgb32"]]},  # says 12 bytes, sends 8
    {"durations": [-1]},
    {"durations": [0, 0]},
    {"metadata": "x"},
    {"metadata": [["a", "b"]]},
    {"full_size": [0, 5]},
    {"exif": 5},  # declared but no blob
    {"frames": []},
])
def test_a_bad_reply_is_refused(qapp, over):
    header, blobs = _good(**over)
    parsed, f = _reply(header, blobs)
    with pytest.raises(protocol.ProtocolError):
        sandbox._read_decoded(parsed, f)


def test_too_many_frames_are_refused(qapp, monkeypatch):
    monkeypatch.setattr(sandbox.config, "MAX_ANIMATION_FRAMES", 2)
    header, _ = _good(frames=[[2, 1, "rgb32"]] * 3, durations=[0, 0, 0])
    parsed, f = _reply(header, [bytes(8)] * 3)
    with pytest.raises(protocol.ProtocolError):
        sandbox._read_decoded(parsed, f)


def test_text_from_a_helper_is_cleaned(qapp):
    header, f = _reply(*_good(metadata=[["Image", "Model", "A\x00B\nC‮D" + "x" * 5000]],
                              format_name="<b>x</b>" * 50))
    image = sandbox._read_decoded(header, f)
    value = image.metadata[0].value
    assert "\x00" not in value and "\n" not in value and "‮" not in value and len(value) <= 2000
    assert len(image.format_name) <= 60


def test_header_limits():
    with pytest.raises(protocol.ProtocolError):
        protocol.read_header(io.BytesIO(struct.pack("<I", protocol.MAX_HEADER + 1)))
    with pytest.raises(protocol.ProtocolError):
        protocol.read_header(io.BytesIO(struct.pack("<I", 3) + b"[1]"))  # not an object
    body = json.dumps({"blobs": [-1]}).encode()
    with pytest.raises(protocol.ProtocolError):
        protocol.read_header(io.BytesIO(struct.pack("<I", len(body)) + body))
    with pytest.raises(protocol.HelperGone):
        protocol.read_header(io.BytesIO(b"\x05\x00"))


def test_pixels_are_read_into_a_new_image(qapp):
    img = QImage(3, 2, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0x80402010)
    hold: list = []
    spec, pixels = protocol.image_parts(img, hold)
    copy = protocol.read_image(io.BytesIO(bytes(pixels)), spec, pixels.nbytes, 100)
    assert copy.pixel(2, 1) == img.pixel(2, 1)
    with pytest.raises(protocol.ProtocolError):
        protocol.read_image(io.BytesIO(bytes(pixels)), spec, pixels.nbytes, 5)  # over the pixel limit


def test_exif_for_saved_copies_is_rebuilt_from_a_whitelist():
    from PIL.TiffImagePlugin import IFDRational

    exif = Image.Exif()
    exif[0x010F] = "Make" + "x" * 500  # kept, capped
    exif[0x010E] = "secret description"  # ImageDescription: not on the list
    exif[0x0131] = "Software"  # not on the list
    exif.get_ifd(0x8769).update({0x829A: IFDRational(1, 125), 0x927C: b"maker note", 0x9286: b"user comment"})
    exif.get_ifd(0x8825).update({1: "N", 2: (IFDRational(55), IFDRational(40), IFDRational(0))})
    clean = Image.Exif()
    clean.load(sandbox.whitelist_exif(exif.tobytes()))
    assert clean[0x010F].startswith("Make") and len(clean[0x010F]) <= 64
    assert 0x010E not in clean and 0x0131 not in clean
    sub = clean.get_ifd(0x8769)
    assert 0x829A in sub and 0x927C not in sub and 0x9286 not in sub
    assert clean.get_ifd(0x8825)[1] == "N"
    assert sandbox.whitelist_exif(b"garbage") is None


# -- the fail-closed rule ----------------------------------------------------------------------------------
@pytest.fixture
def broken_pool(monkeypatch):
    pool = sandbox._Pool()
    pool.state = "new"

    def fail(_role, *_args):
        raise OSError("blocked by policy")

    monkeypatch.setattr(pool, "_spawn", fail)
    monkeypatch.setattr(sandbox, "_pool", pool)
    return pool


def test_never_worked_falls_back_to_this_process(broken_pool, samples, qapp):
    import threading

    broken_pool.worked_before = False
    result = {}
    worker = threading.Thread(target=lambda: result.update(image=sandbox.decode(samples / "sample.png")))
    worker.start()  # off the window thread: there the first start gets the full time and two tries
    worker.join()
    image = result["image"]
    assert image.size.width() == 320 and sandbox.state() == "fallback" and sandbox.in_process()


def test_worked_before_fails_closed(broken_pool, samples, qapp):
    broken_pool.worked_before = True
    with pytest.raises(DecodeError, match="protected decoder"):
        sandbox.decode(samples / "sample.png")
    assert sandbox.state() == "failed" and not sandbox.in_process()
    assert sandbox.make_thumbnail(samples / "sample.png") is None  # no in-process thumbnail either
    with pytest.raises(content.Refused):
        sandbox.check_video(samples / "h264.mp4")
    assert sandbox.video_thumbnail(samples / "h264.mp4") == (None, 0)


# -- real helpers -------------------------------------------------------------------------------------------
@windows
def test_helper_decodes_like_this_process(samples, qapp):
    from mediareaderx import decoders

    for name in ("sample.heic", "sample.avif", "sample.jxl", "sample.psd", "sample.svg", "anim.gif"):
        mine, theirs = decoders.decode(samples / name, 2560), sandbox.decode(samples / name, 2560)
        assert (theirs.size, len(theirs.frames), theirs.format_name) == (mine.size, len(mine.frames), mine.format_name)
        assert theirs.frames[0].pixel(5, 5) == mine.frames[0].pixel(5, 5), name
    assert sandbox.state() == "on"


@windows
def test_helper_is_contained(qapp, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("private", encoding="utf-8")
    assert sandbox.selfcheck(secret) == {"read": "blocked", "network": "blocked", "process": "blocked",
                                         "screen": "blocked", "user_desktop": "blocked"}


@windows
def test_a_helper_that_died_is_replaced(samples, qapp):
    sandbox.decode(samples / "sample.png")
    for helper in list(sandbox._pool.helpers):
        helper.kill()
    from mediareaderx.sandbox import winapi

    for helper in list(sandbox._pool.helpers):
        winapi.wait(helper.proc, 5000)
    assert sandbox.decode(samples / "sample.png").size.width() == 320


@windows
def test_videos_need_allowed_codecs(samples, tmp_path, qapp):
    import av
    import numpy as np

    assert sandbox.check_video(samples / "h264.mp4") is None
    odd = tmp_path / "odd.mkv"
    with av.open(str(odd), "w") as c:
        s = c.add_stream("ffv1", rate=10)
        s.width, s.height, s.pix_fmt = 32, 16, "yuv420p"
        for i in range(3):
            frame = av.VideoFrame.from_ndarray(np.zeros((16, 32, 3), np.uint8), format="rgb24")
            frame.pts = i
            for packet in s.encode(frame):
                c.mux(packet)
        for packet in s.encode():
            c.mux(packet)
    with pytest.raises(content.Refused, match="ffv1"):
        sandbox.check_video(odd)
    frame, duration = sandbox.video_thumbnail(samples / "h264.mp4")
    assert frame is not None and duration > 0
    assert sandbox.video_thumbnail(odd) == (None, 0)


def test_codec_list():
    assert videoprobe.refusal([("video", "hevc"), ("audio", "aac"), ("data", "none")]) is None
    assert videoprobe.refusal([("video", "h264"), ("subtitle", "dvd_subtitle")]) is not None  # decoder opens
    assert videoprobe.refusal([("video", "h264"), ("subtitle", "subrip")]) is None
    assert videoprobe.refusal([("video", "h264"), ("audio", "truehd")]) is not None
    assert videoprobe.refusal([("audio", "aac")]) == videoprobe.DAMAGED  # no picture
    assert videoprobe.refusal([("video", "prores")]) is not None
    assert videoprobe.refusal([("attachment", "ttf"), ("video", "vp9")]) is None


def test_uninstall_entry_point_removes_the_profiles(monkeypatch):
    from mediareaderx import app

    calls = []
    monkeypatch.setattr(sandbox, "remove_profiles", lambda: calls.append(1))
    assert app.main(["--remove-sandbox"]) == 0 and calls == [1]


def test_app_folders_cover_the_code():
    roots = sandbox.app_roots()
    import mediareaderx

    assert any(Path(mediareaderx.__file__).resolve().is_relative_to(r) for r in roots)


# -- review fixes (2026-11-05) -------------------------------------------------------------------------------
def test_malformed_replies_become_protocol_errors(qapp):
    deep = b"[" * 100000 + b"]" * 100000
    with pytest.raises(protocol.ProtocolError):
        protocol.read_header(io.BytesIO(struct.pack("<I", len(deep)) + deep))
    with pytest.raises(protocol.ProtocolError):
        protocol.read_image(io.BytesIO(bytes(8)), [2, 1, ["rgb32"]], 8, 100)  # a list where a name belongs


def test_gps_survives_the_whitelist_when_kept():
    from PIL.TiffImagePlugin import IFDRational

    exif = Image.Exif()
    exif.get_ifd(0x8825).update({0: b"\x02\x02\x00\x00", 1: "N", 2: (IFDRational(55), IFDRational(40), IFDRational(0)),
                                 5: b"\x00"})
    clean = Image.Exif()
    clean.load(sandbox.whitelist_exif(exif.tobytes()))
    gps = clean.get_ifd(0x8825)
    assert 2 in gps and 0 in gps and 5 in gps


def test_a_working_session_fails_closed(monkeypatch, samples, qapp):
    pool = sandbox._Pool()
    pool.state, pool.worked_before = "new", False
    monkeypatch.setattr(sandbox, "_pool", pool)
    pool._set_state("on")  # a helper worked in this session
    monkeypatch.setattr(pool, "_spawn", lambda role, *a: (_ for _ in ()).throw(OSError("blocked")))
    with pytest.raises(DecodeError):
        sandbox.decode(samples / "sample.png")
    assert sandbox.state() == "failed"


def test_all_slots_taken_waits_and_is_not_a_failure(monkeypatch, samples, qapp):
    pool = sandbox._Pool()
    pool.state = "new"
    monkeypatch.setattr(sandbox, "_pool", pool)
    monkeypatch.setattr(sandbox, "ACQUIRE_WAIT_S", 1)
    monkeypatch.setattr(pool, "_spawn", lambda role, *a: (_ for _ in ()).throw(sandbox.NoSlot("taken")))
    with pytest.raises(DecodeError, match="busy"):
        sandbox.decode(samples / "sample.png")
    assert sandbox.state() == "new"


@windows
def test_slot_claims_are_exclusive(tmp_path):
    from mediareaderx.sandbox import winapi

    CLAIM = tmp_path / "slots" / "0.lock"  # noqa: N806
    first = winapi.claim_slot(CLAIM)
    assert first is not None
    assert winapi.claim_slot(CLAIM) is None
    winapi.close(first)
    again = winapi.claim_slot(CLAIM)
    assert again is not None
    winapi.close(again)
    assert not CLAIM.exists()  # the lock file goes with the handle


def test_a_refusal_from_the_helper_keeps_its_reason(monkeypatch, samples, qapp):
    def fake_request(role, op, path, extra, timeout, read_reply):
        return read_reply({"ok": True, "refused": "This video uses a format that Media Reader x does not play (x).",
                           "blobs": []}, None)

    monkeypatch.setattr(sandbox, "_request", fake_request)
    monkeypatch.setattr(sandbox._pool, "state", "on")
    with pytest.raises(content.Refused, match="does not play"):
        sandbox.check_video(samples / "h264.mp4")


def test_fallback_never_follows_a_working_helper():
    pool = sandbox._Pool()
    pool.state = "new"
    pool._set_state("on")
    pool._set_state("fallback")  # a failing start that raced a working one
    assert pool.state == "failed"


def test_clean_text_removes_c1_and_separators():
    assert protocol.clean_text("a" + chr(0x85) + "b" + chr(0x2028) + "c" + chr(0x61C)) == "a b c "
    assert protocol.clean_text("x" + chr(0x200B) + chr(0xFEFF) + chr(0x2062) + "y") == "x   y"


def test_more_than_32_streams_are_refused(tmp_path, qapp):
    """1.6.0 probed with max_streams=32, so FFmpeg hid stream 33 from the check but not from the player."""
    import av
    import numpy as np

    path = tmp_path / "many.mkv"
    with av.open(str(path), "w") as c:
        video = c.add_stream("mpeg4", rate=10)
        video.width, video.height, video.pix_fmt = 32, 16, "yuv420p"
        audio = [c.add_stream("mp2", rate=48000) for _ in range(33)]
        for i in range(3):
            frame = av.VideoFrame.from_ndarray(np.zeros((16, 32, 3), np.uint8), format="rgb24")
            frame.pts = i
            for packet in video.encode(frame):
                c.mux(packet)
        for packet in video.encode():
            c.mux(packet)
        for stream in audio:
            for packet in stream.encode():
                c.mux(packet)
    head = content.read_head(path)
    size = path.stat().st_size
    with path.open("rb") as f:
        found = videoprobe.streams(f, size, content.ffmpeg_demuxer(head, ".mkv", size <= len(head)) or "")
    assert len(found) > videoprobe.MAX_STREAMS
    assert "more than 32 streams" in videoprobe.refusal(found)
    with pytest.raises(content.Refused, match="32 streams"):
        sandbox.check_video(path)


def test_late_streams_are_scanned_for_dynamic_containers():
    assert videoprobe.scan_bytes_for("mpegts") > 0 and videoprobe.scan_bytes_for("mpeg") > 0
    assert videoprobe.scan_bytes_for("mov,mp4,m4a,3gp,3g2,mj2") == 0


def test_replies_larger_than_asked_are_refused(qapp):
    header, blobs = _good(frames=[[4000, 1, "rgb32"]], full_size=[8000, 2])  # a screen copy (reduced)
    parsed, f = _reply(header, [bytes(16000)])
    with pytest.raises(protocol.ProtocolError, match="larger than asked"):
        sandbox._read_decoded(parsed, f, max_side=2560)
    parsed, f = _reply({"ok": True, "frames": [[500, 2, "rgb32"]]}, [bytes(4000)])
    with pytest.raises(protocol.ProtocolError, match="larger than asked"):
        sandbox._read_thumb(parsed, f, 192)


def test_fallback_thumbnailer_checks_codecs():
    from mediareaderx import thumbnails

    seen = []
    thumb = thumbnails.VideoThumbnailer(check=lambda p: seen.append(p))
    assert thumb._check is not None
    from mediareaderx import video_thumbs

    assert "check_video_in_process" in open(video_thumbs.__file__, encoding="utf-8").read()


def test_helper_notes_never_reach_the_log():
    """A helper's notes can carry text from the file (or what a taken-over helper saw): only their count is logged."""
    source = Path(sandbox.__file__).read_text(encoding="utf-8")
    assert '"Decoder: %s"' not in source and "Decoder warnings: %d" in source


# -- 1.6.1 correctness review ----------------------------------------------------------------------------
@windows
def test_large_svg_opens_as_a_screen_copy_request(tmp_path, qapp):
    """1.6.1 first rejected any SVG rendered wider than the screen copy size (SVG ignores max_side)."""
    path = tmp_path / "wide.svg"
    path.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 800"><rect width="1000" height="800" '
                    'fill="#2a6"/></svg>', encoding="utf-8")
    image = sandbox.decode(path, 1920)
    assert image.frames[0].width() == 2000


def test_one_failed_start_keeps_working_helpers(monkeypatch):
    pool = sandbox._Pool()
    pool.state = "on"

    class Live:
        proc = 1

    pool.helpers.append(Live())
    pool._start_failed(OSError("one slow start"))
    assert pool.state == "on"
    pool.helpers.clear()
    pool._start_failed(OSError("none left"))
    assert pool.state == "failed"


def test_fallback_thumbnailer_is_dropped_when_protection_returns(monkeypatch, qapp):
    from mediareaderx import video_thumbs

    thumbs = video_thumbs.ProtectedVideoThumbnailer()

    class FakeFallback:
        _queue = ["a.mp4"]
        current = None

        def clear(self):
            pass

        def release(self, path):
            pass

        def deleteLater(self):
            pass

        def request(self, path):
            raise AssertionError("must not be used once protection is on")

    thumbs._fallback = FakeFallback()
    failed = []
    thumbs.failed.connect(failed.append)
    monkeypatch.setattr(sandbox, "in_process", lambda: False)
    monkeypatch.setattr(thumbs, "_next", lambda: None)
    thumbs.request("b.mp4")
    assert thumbs._fallback is None and failed == ["a.mp4"]


def test_a_slow_first_start_on_the_window_thread_is_not_a_failure(broken_pool, samples, qapp, monkeypatch):
    broken_pool.worked_before = False
    monkeypatch.setattr(broken_pool, "prewarm", lambda *a, **k: None)
    with pytest.raises(DecodeError, match="busy"):
        sandbox.decode(samples / "sample.png")
    assert sandbox.state() == "new"  # the background start decides, not a 25 s wait on the window thread


def test_exactly_32_allowed_streams_are_still_analysed(tmp_path, qapp, monkeypatch):
    """The early (header) check refused only for sure; 32 allowed streams go on to FFmpeg's full analysis."""
    import av
    import numpy as np

    path = tmp_path / "thirtytwo.mkv"
    with av.open(str(path), "w") as c:
        video = c.add_stream("mpeg4", rate=10)
        video.width, video.height, video.pix_fmt = 32, 16, "yuv420p"
        audio = [c.add_stream("mp2", rate=48000) for _ in range(31)]
        for i in range(3):
            frame = av.VideoFrame.from_ndarray(np.zeros((16, 32, 3), np.uint8), format="rgb24")
            frame.pts = i
            for packet in video.encode(frame):
                c.mux(packet)
        for packet in video.encode():
            c.mux(packet)
        for stream in audio:
            for packet in stream.encode():
                c.mux(packet)
    calls = []
    real = videoprobe._ffmpeg()
    wrapped = dict(real)
    wrapped["find_info"] = lambda *a: calls.append(1) or real["find_info"](*a)
    monkeypatch.setattr(videoprobe, "_ffmpeg", lambda: wrapped)
    head = content.read_head(path)
    size = path.stat().st_size
    with path.open("rb") as f:
        found = videoprobe.streams(f, size, content.ffmpeg_demuxer(head, ".mkv", size <= len(head)) or "")
    assert len(found) == 32 and calls == [1] and videoprobe.refusal(found) is None
