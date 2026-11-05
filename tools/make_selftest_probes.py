"""Write the tiny HEIC, AVIF and MP4 files the self-test and codec warm-up decode.

They are committed under src/mediareaderx/assets, so the shipped app only ever decodes HEVC and AV1:
it never runs the x265 or libaom encoders bundled in the pillow-heif and Pillow wheels. The MP4
(MPEG-4 Part 2 video, AAC sound, made with PyAV, a development tool) lets the self-test check that the
FFmpeg stream reader in videoprobe.py still matches the bundled FFmpeg.
Run again only if the probe colour or size in selftest.py changes.
Usage: python tools/make_selftest_probes.py
"""

from __future__ import annotations

from pathlib import Path

import pillow_heif
from PIL import Image

PROBE_SIZE = (16, 8)
PROBE_RGB = (200, 50, 50)
ASSETS = Path(__file__).resolve().parents[1] / "src" / "mediareaderx" / "assets"


def _video(path: Path) -> None:
    from fractions import Fraction

    import av
    import numpy as np

    with av.open(str(path), "w") as c:
        vs = c.add_stream("mpeg4", rate=10)
        vs.width, vs.height, vs.pix_fmt = 32, 16, "yuv420p"
        aud = c.add_stream("aac", rate=48000)
        aud.layout = "mono"
        for i in range(5):
            frame = av.VideoFrame.from_ndarray(np.full((16, 32, 3), PROBE_RGB, np.uint8), format="rgb24")
            frame.pts = i
            for packet in vs.encode(frame):
                c.mux(packet)
        for packet in vs.encode():
            c.mux(packet)
        samples = aud.frame_size or 1024
        for k in range(4):
            af = av.AudioFrame.from_ndarray(np.zeros((1, samples), np.float32), format="fltp", layout="mono")
            af.sample_rate, af.pts, af.time_base = 48000, k * samples, Fraction(1, 48000)
            for packet in aud.encode(af):
                c.mux(packet)
        for packet in aud.encode():
            c.mux(packet)


def main() -> None:
    _video(ASSETS / "probe.mp4")
    im = Image.new("RGB", PROBE_SIZE, PROBE_RGB)
    pillow_heif.from_pillow(im).save(ASSETS / "probe.heic", quality=95)
    im.save(ASSETS / "probe.avif", quality=95)
    for name in ("probe.heic", "probe.avif", "probe.mp4"):
        print(name, (ASSETS / name).stat().st_size, "bytes")


if __name__ == "__main__":
    main()
