"""Write the tiny HEIC and AVIF files the self-test and codec warm-up decode.

They are committed under src/mediareaderx/assets, so the shipped app only ever decodes HEVC and AV1:
it never runs the x265 or libaom encoders bundled in the pillow-heif and Pillow wheels.
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


def main() -> None:
    im = Image.new("RGB", PROBE_SIZE, PROBE_RGB)
    pillow_heif.from_pillow(im).save(ASSETS / "probe.heic", quality=95)
    im.save(ASSETS / "probe.avif", quality=95)
    for name in ("probe.heic", "probe.avif"):
        print(name, (ASSETS / name).stat().st_size, "bytes")


if __name__ == "__main__":
    main()
