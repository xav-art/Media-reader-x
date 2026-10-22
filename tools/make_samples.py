"""Generate small synthetic media samples used by the test suite.

Creates one file per supported family (HEIC, AVIF, JXL, animated WebP/GIF, SVG,
HEVC/H.264/VP9/AV1 video, and an iPhone-style Live Photo pair) so tests never
depend on personal photos. Usage: python tools/make_samples.py <output_dir>
"""

from __future__ import annotations

import sys
from fractions import Fraction
from pathlib import Path

import av
import imagecodecs
import numpy as np
import pillow_heif
from PIL import Image, ImageDraw

W, H = 320, 240


def _frame(i: int) -> Image.Image:
    img = Image.new("RGB", (W, H), (20, 40 + i * 4 % 200, 90))
    d = ImageDraw.Draw(img)
    d.rectangle([10 + i * 5, 60, 70 + i * 5, 120], fill=(240, 200, 30))
    d.text((10, 10), f"frame {i}", fill=(255, 255, 255))
    return img


def _make_psd(path: Path, base, depth: int = 8, merged: bool = True) -> None:
    """A layered Photoshop file: the sample as the bottom layer and a box on top.

    ``merged=False`` marks the flattened image as missing, like Photoshop with
    "Maximize compatibility" off.
    """
    from PIL import Image
    from psd_tools import PSDImage
    from psd_tools.constants import Resource

    psd = PSDImage.new("RGB", base.size, color=(255, 255, 255), depth=depth)
    psd.create_pixel_layer(base.convert("RGB"), name="Photo")
    psd.create_pixel_layer(Image.new("RGB", (40, 30), (250, 250, 20)), name="Box", top=10, left=10)
    if not merged:
        info = psd.image_resources.get_data(Resource.VERSION_INFO)
        info.has_composite = False
    psd.save(path)


def make_images(out: Path) -> None:
    base = _frame(0)
    exif = Image.Exif()
    exif[0x010F] = "SampleMake"  # Make
    exif[0x0110] = "SampleCam"  # Model
    exif[0x0132] = "2026:10:21 12:00:00"  # DateTime
    pillow_heif.register_heif_opener()
    base.save(out / "sample.heic", exif=exif.tobytes(), quality=80)
    base.save(out / "sample.avif", quality=60)
    (out / "sample.jxl").write_bytes(imagecodecs.jpegxl_encode(np.asarray(base), level=90))
    frames = [_frame(i) for i in range(8)]
    frames[0].save(out / "anim.webp", save_all=True, append_images=frames[1:], duration=80, loop=0)
    frames[0].save(out / "anim.gif", save_all=True, append_images=frames[1:], duration=80, loop=0)
    base.save(out / "sample.png")
    _make_psd(out / "sample.psd", base, depth=16)  # 16-bit: the case Pillow alone cannot open
    (out / "sample.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">'
        '<rect width="200" height="100" fill="#2a6"/><circle cx="50" cy="50" r="30" fill="#fc3"/></svg>',
        encoding="utf-8",
    )


def make_video(path: Path, codec: str, container_fmt: str | None = None, audio: str | None = "aac") -> None:
    rate = 24
    with av.open(str(path), "w", format=container_fmt) as c:
        vs = c.add_stream(codec, rate=rate)
        vs.width, vs.height, vs.pix_fmt = W, H, "yuv420p"
        a_s = c.add_stream(audio, rate=48000) if audio else None
        if a_s is not None:
            a_s.layout = "stereo"
        for i in range(rate * 2):
            frame = av.VideoFrame.from_image(_frame(i))
            for p in vs.encode(frame):
                c.mux(p)
        for p in vs.encode():
            c.mux(p)
        if a_s is not None:
            samples = a_s.frame_size or 1024
            t = 0
            for _ in range(48000 * 2 // samples):
                tone = (0.2 * np.sin(2 * np.pi * 440 * (np.arange(samples) + t) / 48000)).astype("float32")
                af = av.AudioFrame.from_ndarray(np.stack([tone, tone]), format="fltp", layout="stereo")
                af.sample_rate, af.pts, af.time_base = 48000, t, Fraction(1, 48000)
                t += samples
                for p in a_s.encode(af):
                    c.mux(p)
            for p in a_s.encode():
                c.mux(p)


def make_videos(out: Path) -> None:
    make_video(out / "hevc.mp4", "libx265")
    make_video(out / "hevc.mov", "libx265", "mov")
    make_video(out / "h264.mp4", "libx264")
    make_video(out / "vp9.webm", "libvpx-vp9", audio="libopus")
    make_video(out / "av1.mkv", "libsvtav1", audio="libopus")
    # iPhone Live Photo: IMG_0001.HEIC + IMG_0001.MOV with the same stem.
    _frame(0).save(out / "IMG_0001.HEIC")
    make_video(out / "IMG_0001.MOV", "libx265", "mov")


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "tests/samples")
    target.mkdir(parents=True, exist_ok=True)
    make_images(target)
    make_videos(target)
    print("samples written to", target.resolve())
