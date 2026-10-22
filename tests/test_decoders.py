import pytest
from PIL import Image

from mediareaderx import config
from mediareaderx.decoders import DecodeError, decode


@pytest.mark.parametrize("name, fmt", [
    ("sample.heic", "HEIF"), ("sample.avif", "AVIF"), ("sample.jxl", "JPEG XL"),
    ("sample.png", "PNG"), ("sample.svg", "SVG"), ("IMG_0001.HEIC", "HEIF"),
])
def test_still_formats_decode(samples, qapp, name, fmt):
    img = decode(samples / name)
    assert img.format_name == fmt
    assert not img.frames[0].isNull()
    assert img.size.width() > 0 and img.size.height() > 0


@pytest.mark.parametrize("name", ["anim.gif", "anim.webp"])
def test_animations_keep_all_frames(samples, qapp, name):
    img = decode(samples / name)
    assert img.animated and len(img.frames) == 8
    assert all(d >= 20 for d in img.durations_ms)


def test_heic_exif_is_reported(samples, qapp):
    rows = {(m.section, m.key): m.value for m in decode(samples / "sample.heic").metadata}
    assert rows[("Camera", "Camera make")] == "SampleMake"
    assert rows[("Camera", "Camera model")] == "SampleCam"
    assert rows[("Image", "Dimensions")] == "320 x 240 px"
    assert ("File", "Size") in rows


def test_exif_orientation_is_applied(tmp_path, qapp):
    path = tmp_path / "rotated.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 CW
    Image.new("RGB", (40, 20), "red").save(path, exif=exif.tobytes())
    img = decode(path)
    assert (img.size.width(), img.size.height()) == (20, 40)


def test_16bit_png_is_normalised(tmp_path, qapp):
    path = tmp_path / "deep.png"
    Image.new("I;16", (8, 8), 40000).save(path)
    assert not decode(path).frames[0].isNull()


def test_damaged_file_gives_friendly_error(tmp_path, qapp):
    path = tmp_path / "broken.heic"
    path.write_bytes(b"this is not an image" * 10)
    with pytest.raises(DecodeError, match="damaged"):
        decode(path)


def test_empty_file_rejected(tmp_path, qapp):
    path = tmp_path / "empty.png"
    path.touch()
    with pytest.raises(DecodeError, match="empty"):
        decode(path)


def test_size_limit_enforced(samples, qapp, monkeypatch):
    monkeypatch.setattr(config, "MAX_IMAGE_FILE_BYTES", 10)
    with pytest.raises(DecodeError, match="safety limit"):
        decode(samples / "sample.png")


def test_decompression_bomb_rejected(tmp_path, qapp, monkeypatch):
    path = tmp_path / "big.png"
    Image.new("L", (300, 300)).save(path)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    with pytest.raises(DecodeError):
        decode(path)


def test_missing_file(tmp_path, qapp):
    with pytest.raises(DecodeError, match="cannot be read"):
        decode(tmp_path / "nope.jpg")


def test_video_extension_is_not_an_image(samples, qapp):
    with pytest.raises(DecodeError):
        decode(samples / "hevc.mp4")


def test_raw_decoder_reports_bad_raw(tmp_path, qapp):
    path = tmp_path / "fake.cr2"
    path.write_bytes(b"\x00" * 4096)
    with pytest.raises(DecodeError, match="RAW"):
        decode(path)
