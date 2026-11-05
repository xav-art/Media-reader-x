"""1.5.0: each file reaches exactly one decoder, chosen from its content (content.py).

Covers the dispatch table, mismatch notices, refusals, the video container check and the SVG picture check.
"""

import base64
import gzip
import io

import pytest
from PIL import Image

from mediareaderx import content
from mediareaderx.content import Refused, check_video, load_svg, sniff
from mediareaderx.decoders import PILLOW_PLUGINS, DecodeError, decode, register_plugins
from mediareaderx.formats import IMAGE_EXTS, IMAGE_FAMILIES, VIDEO_EXTS, VIDEO_FAMILIES
from mediareaderx.resources import resource_dir
from mediareaderx.thumbnails import make_thumbnail


def _png_bytes(size=(6, 4), colour=(0, 0, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "PNG")
    return buf.getvalue()


# -- tables ------------------------------------------------------------------------------------------
def test_every_extension_has_families_and_every_plugin_is_registered(qapp):
    register_plugins()
    assert set(IMAGE_FAMILIES) == set(IMAGE_EXTS)
    assert set(VIDEO_FAMILIES) == set(VIDEO_EXTS)
    for families in IMAGE_FAMILIES.values():
        for family in families:
            assert family in PILLOW_PLUGINS or family in ("jxl", "psd"), family
            assert family in content.FAMILIES
    Image.init()  # Pillow loads its plugins lazily; Image.open(formats=...) does the same before it looks
    for name in PILLOW_PLUGINS.values():
        assert name in Image.OPEN, name  # a missing name would raise KeyError, not a clean refusal
    for families in VIDEO_FAMILIES.values():
        assert all(content.FAMILIES[f].kind == "video" for f in families)


def test_heif_writer_is_unregistered_and_limits_are_on(qapp):
    import pillow_heif

    register_plugins()
    assert "HEIF" not in Image.SAVE and "HEIF" not in Image.SAVE_ALL
    assert pillow_heif.options.DISABLE_SECURITY_LIMITS is False


# -- images: one decoder, chosen from the content -------------------------------------------------
@pytest.mark.parametrize("name,fmt", [("png.heic", "PNG"), ("png.jpg", "PNG"), ("png.ico", "PNG")])
def test_mismatched_picture_opens_with_its_own_decoder_and_a_notice(tmp_path, qapp, name, fmt):
    path = tmp_path / name
    path.write_bytes(_png_bytes())
    img = decode(path)
    assert img.format_name == fmt
    assert img.notice == f"PNG file named {path.suffix}"


def test_webp_saved_as_jpg_opens(tmp_path, qapp):
    path = tmp_path / "download.jpg"
    Image.new("RGB", (8, 8), (10, 200, 10)).save(path, "WEBP")
    img = decode(path)
    assert img.format_name == "WEBP" and img.notice == "WebP file named .jpg"


def test_heic_named_jpg_opens_as_heic(samples, tmp_path, qapp):
    path = tmp_path / "phone.jpg"
    path.write_bytes((samples / "sample.heic").read_bytes())
    img = decode(path)
    assert img.format_name == "HEIF" and img.notice == "HEIC file named .jpg"


def test_matching_files_have_no_notice(samples, qapp):
    for name in ("sample.heic", "sample.avif", "sample.png", "sample.jxl", "anim.webp", "anim.gif"):
        assert decode(samples / name).notice is None, name


def test_avif_with_generic_mif1_brand_opens_under_heif(tmp_path, qapp):
    data = bytearray((resource_dir() / "probe.avif").read_bytes())
    assert data[8:12] == b"avif"
    data[8:12] = b"mif1"  # spec-valid AVIF: generic major brand, avif only among the compatible brands
    path = tmp_path / "android.heif"
    path.write_bytes(bytes(data))
    img = decode(path)
    assert img.format_name == "AVIF" and img.notice is None


def test_heif_with_avc_compression_says_why(tmp_path, qapp):
    path = tmp_path / "avc.heif"
    path.write_bytes(b"\x00\x00\x00\x18ftypavci\x00\x00\x00\x00mif1avci" + bytes(64))
    with pytest.raises(DecodeError, match="AVC"):
        decode(path)


@pytest.mark.parametrize("data,words", [
    (b"MZ" + bytes(200), "a Windows program"),
    (b"PK\x03\x04" + bytes(200), "a ZIP archive"),
    (b"%PDF-1.7\n" + bytes(200), "a PDF document"),
    (b"%!PS-Adobe-3.0 EPSF-3.0\n" * 10, "a text file"),  # EPS: Pillow's EPS plugin (Ghostscript) stays unused
])
def test_non_pictures_are_refused(tmp_path, qapp, data, words):
    path = tmp_path / "photo.heic"
    path.write_bytes(data)
    with pytest.raises(DecodeError, match=words):
        decode(path)


def test_unknown_binary_never_reaches_other_pillow_plugins(tmp_path, qapp):
    # A placeable WMF header: Pillow would play it through Windows GDI if it could choose the plugin.
    path = tmp_path / "picture.png"
    path.write_bytes(b"\xd7\xcd\xc6\x9a" + bytes(200))
    with pytest.raises(DecodeError, match="damaged"):
        decode(path)


def test_video_named_as_picture_says_how_to_open_it(samples, tmp_path, qapp):
    path = tmp_path / "clip.jpg"
    path.write_bytes((samples / "h264.mp4").read_bytes())
    with pytest.raises(DecodeError, match=r"change the extension to \.mp4"):
        decode(path)


def test_tga_opens_by_extension(tmp_path, qapp):
    path = tmp_path / "texture.tga"
    Image.new("RGB", (5, 3), (1, 2, 3)).save(path, "TGA")
    assert decode(path).format_name == "TGA"


def test_message_never_contains_file_bytes(tmp_path, qapp):
    path = tmp_path / "x.heic"
    path.write_bytes(b"\x00\x00\x00\x18ftypEVIL\x00\x00\x00\x00EVILEVIL" + bytes(64))
    with pytest.raises(DecodeError) as err:
        decode(path)
    assert "EVIL" not in str(err.value)


def test_thumbnails_use_the_same_lock(tmp_path, qapp):
    png = tmp_path / "png.heic"
    png.write_bytes(_png_bytes((40, 20)))
    assert make_thumbnail(png) is not None
    program = tmp_path / "program.png"
    program.write_bytes(b"MZ" + bytes(500))
    assert make_thumbnail(program) is None


# -- video: a known container from the first byte ------------------------------------------------
def test_real_videos_pass(samples):
    for name in ("hevc.mp4", "hevc.mov", "h264.mp4", "vp9.webm", "av1.mkv", "IMG_0001.MOV"):
        assert check_video(samples / name) is None, name


def test_mkv_named_mp4_plays_with_notice(samples, tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes((samples / "av1.mkv").read_bytes())
    assert check_video(path) == "MKV/WebM file named .mp4"


@pytest.mark.parametrize("data", [
    b"#EXTM3U\n#EXT-X-VERSION:3\nhttp://example.invalid/a.ts\n",  # HLS playlist: FFmpeg would open the URL
    b"ffconcat version 1.0\nfile 'C:/Windows/x.mp4'\n",  # concat: reads other files
    b"v=0\no=- 0 0 IN IP4 127.0.0.1\n",  # SDP: opens network streams
    b"\x89PNG\r\n\x1a\n" + bytes(200),  # an image-pipe demuxer would decode it
    b"\x76\x2f\x31\x01" + bytes(200),  # OpenEXR magic: a decoder the app does not support
])
def test_non_video_content_is_refused(tmp_path, data):
    path = tmp_path / "clip.mp4"
    path.write_bytes(data)
    with pytest.raises(Refused):
        check_video(path)


def _ts(stride: int, start: int = 0, packets: int = 8) -> bytes:
    data = bytearray(stride * packets)
    for k in range(packets):
        data[start + k * stride] = 0x47
    return bytes(data)


@pytest.mark.parametrize("name,data", [
    ("old.mov", b"\x00\x00\x00\x08wide" + b"\x00\x00\x00\x10mdat" + bytes(64)),
    ("sony.mp4", b"\x00\x00\x00\x20uuid" + bytes(24) + b"\x00\x00\x00\x18ftypmp42" + bytes(64)),
    ("dash.mp4", b"\x00\x00\x00\x18stypmsdh\x00\x00\x00\x00msdhmsix" + bytes(64)),
], ids=["wide-first", "uuid-first", "styp-first"])
def test_mov_atom_variants_pass(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    assert check_video(path) is None


@pytest.mark.parametrize("name,codec,fmt,options", [
    ("broadcast.ts", "mpeg2video", "mpegts", {}),
    ("camera.m2ts", "mpeg2video", "mpegts", {"mpegts_m2ts_mode": "1"}),
    ("dvr.mpg", "mpeg2video", "mpegts", {}),
    ("dvd.mpg", "mpeg2video", "vob", {}),
    ("film.avi", "mpeg4", "avi", {}),
    ("film.wmv", "wmv2", "asf", {}),
    ("film.flv", "flv", "flv", {}),
    ("film.ogv", "libvpx", "ogg", {}),
], ids=["ts", "m2ts", "ts-as-mpg", "ps", "avi", "wmv", "flv", "ogv"])
def test_real_containers_pass(tmp_path, name, codec, fmt, options):
    import av

    path = tmp_path / name
    try:
        with av.open(str(path), "w", format=fmt, options=options) as out:
            stream = out.add_stream(codec, rate=24)
            stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
            for i in range(24):
                frame = av.VideoFrame.from_image(Image.new("RGB", (64, 48), (i * 10, 50, 90)))
                for packet in stream.encode(frame):
                    out.mux(packet)
            for packet in stream.encode():
                out.mux(packet)
    except (av.FFmpegError, ValueError) as exc:  # an encoder missing from the dev-only PyAV build
        pytest.skip(f"PyAV cannot write {fmt}/{codec}: {exc}")
    assert check_video(path) is None


@pytest.mark.parametrize("data", [
    # Pass a signature check, but FFmpeg's probe prefers another demuxer (diff review, verified with FFmpeg 9):
    b"\x00\x00\x00\x14ftypjp2 \x00\x00\x00\x00jp2 v=0\no=- 0 0 IN IP4 127.0.0.1\ns=x\nc=IN IP4 127.0.0.1\n"
    b"m=video 5004 RTP/AVP 96\n" + bytes(64),  # SDP: would open rtp://
    b"\x00\x00\x00\x20free" + bytes(24) + bytes.fromhex("060e2b34020501010d01020101020400") + bytes(512),  # MXF
])
def test_ffmpeg_must_pick_the_same_container(tmp_path, data):
    path = tmp_path / "clip.mp4"
    path.write_bytes(data)
    with pytest.raises(Refused):
        check_video(path)


def test_ffmpeg_probe_agrees_on_real_videos(samples):
    expected = {"hevc.mp4": "mov,mp4,m4a,3gp,3g2,mj2", "vp9.webm": "matroska,webm", "av1.mkv": "matroska,webm"}
    for name, demuxer in expected.items():
        head = (samples / name).read_bytes()[:content.HEAD_BYTES]
        assert content.ffmpeg_demuxer(head, (samples / name).suffix, at_end=True) == demuxer


def test_ffmpeg_may_only_open_local_files():
    import os

    import mediareaderx  # noqa: F401  (sets the variable at import)

    assert os.environ["QT_FFMPEG_PROTOCOL_WHITELIST"] == "file"


def test_ts_cut_mid_packet_is_refused(tmp_path):
    path = tmp_path / "cut.ts"
    path.write_bytes(b"\x76\x2f\x31\x01" + _ts(188, 30))  # EXR magic first, sync later: not from byte 0
    with pytest.raises(Refused):
        check_video(path)


# -- SVG: only embedded PNG/JPEG pictures, never links ---------------------------------------------
SVG_HEAD = ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            'width="8" height="8">')


def _svg(tmp_path, body: str, name="drawing.svg") -> object:
    path = tmp_path / name
    path.write_text(SVG_HEAD + body + "</svg>", encoding="utf-8")
    return path


@pytest.mark.parametrize("body", [
    '<image href="C:/Windows/Web/Wallpaper/Windows/img0.jpg" width="8" height="8"/>',
    '<image xlink:href="neighbour.png" width="8" height="8"/>',
    '<image href="https://example.invalid/a.png" width="8" height="8"/>',
    '<image href="file:///C:/x.png" width="8" height="8"/>',
    '<filter id="f"><feImage href="x.png"/></filter>',
    '<image href="data:image/png,%89PNG" width="8" height="8"/>',  # not base64
    '<image href="data:image/png;base64,SUkqAAgAAAA=" width="8" height="8"/>',  # TIFF labelled PNG
    '<use href="other.svg#shape"/>',
])
def test_svg_links_and_other_pictures_are_refused(tmp_path, qapp, body):
    path = _svg(tmp_path, body)
    with pytest.raises(Refused):
        load_svg(path)
    with pytest.raises(DecodeError):
        decode(path)
    assert make_thumbnail(path) is None


@pytest.mark.parametrize("href", ["#neighbour.png", "DATA:image/png;base64,AAAA", " data:image/png;base64,AAAA"])
def test_svg_image_must_be_data_exactly_as_qt_reads_it(tmp_path, qapp, monkeypatch, href):
    # Qt opens any <image> href not starting with "data" as a file path, relative to the working folder.
    (tmp_path / "#neighbour.png").write_bytes(_png_bytes((8, 8), (255, 0, 0)))
    monkeypatch.chdir(tmp_path)
    path = _svg(tmp_path, f'<image href="{href}" width="8" height="8"/>')
    with pytest.raises(Refused):
        load_svg(path)


def test_svg_with_undeclared_xlink_prefix_still_opens(tmp_path, qapp):
    data = base64.b64encode(_png_bytes((8, 8), (255, 0, 0))).decode()
    path = tmp_path / "old.svg"
    path.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8">'
                    f'<image xlink:href="data:image/png;base64,{data}" width="8" height="8"/></svg>', encoding="utf-8")
    assert decode(path).frames


def test_headerless_dib_named_bmp_opens(tmp_path, qapp):
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (9, 9, 9)).save(buf, "BMP")
    path = tmp_path / "raw.bmp"
    path.write_bytes(buf.getvalue()[14:])  # a DIB: the BMP without its 14-byte file header
    assert decode(path).format_name == "DIB"


def test_svg_entity_declarations_are_refused(tmp_path, qapp):
    path = tmp_path / "entity.svg"
    path.write_text('<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY i "<image href=\'x.png\'/>">]>'
                    + SVG_HEAD + "&i;</svg>", encoding="utf-8")
    with pytest.raises(Refused):
        load_svg(path)


def test_svg_with_embedded_png_and_local_reference_opens(tmp_path, qapp):
    data = base64.b64encode(_png_bytes((8, 8), (255, 0, 0))).decode()
    path = _svg(tmp_path, f'<defs><rect id="r" width="8" height="8" fill="#00f"/></defs><use href="#r"/>'
                          f'<image href="data:image/png;base64,{data}" width="8" height="8"/>')
    img = decode(path)
    assert not img.frames[0].isNull()
    assert (img.frames[0].pixel(4, 4) >> 16) & 0xFF > 200  # the embedded red PNG was drawn


def test_svgz_is_inflated_with_a_cap(tmp_path, qapp, monkeypatch):
    path = tmp_path / "drawing.svgz"
    path.write_bytes(gzip.compress((SVG_HEAD + '<rect width="8" height="8"/></svg>').encode()))
    assert decode(path).frames
    monkeypatch.setattr(content, "MAX_SVG_BYTES", 32)
    with pytest.raises(DecodeError, match="too large"):
        decode(path)


def test_sniff_recognises_svg_and_ignores_plain_xml():
    assert sniff(b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"/>').key == "svg"
    assert sniff(b'<?xml version="1.0"?>\n<note>hello</note>').key == "text"


# -- window --------------------------------------------------------------------------------------
def test_window_shows_the_notice_and_refuses_fake_video(tmp_path, qapp):
    from PySide6.QtCore import QCoreApplication, QElapsedTimer

    from mediareaderx.main_window import MainWindow

    def wait_until(predicate, timeout_ms=8000):
        timer = QElapsedTimer()
        timer.start()
        while not predicate():
            if timer.elapsed() > timeout_ms:
                return False
            QCoreApplication.processEvents()
        return True

    w = MainWindow()
    w.show()
    try:
        png = tmp_path / "photo.heic"
        png.write_bytes(_png_bytes((30, 20)))
        w.open_path(png)
        assert wait_until(lambda: w.stack.currentIndex() == w.PAGE_IMAGE)
        assert "PNG file named .heic" in w.status.text()
        fake = tmp_path / "clip.mp4"
        fake.write_bytes(b"#EXTM3U\nhttp://example.invalid/a.ts\n")
        w.open_path(fake)
        assert w.stack.currentIndex() == w.PAGE_MESSAGE
        assert "not a video" in w.message.text()
        assert w.video_view.player.source().isEmpty()  # FFmpeg never saw the file
    finally:
        w.close()
