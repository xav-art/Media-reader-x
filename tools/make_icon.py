"""Render the multi-size app icon (.ico) from src/mediareaderx/assets/logo.svg.

Sizes of 48 px and up use the full "Pixel x" artwork. At 32 px and below a
pixel-snapped variant is used (each pixel lands on whole device pixels, no play
triangle) so the x stays crisp in the taskbar and Explorer.

Usage: python tools/make_icon.py src/mediareaderx/assets/app.ico
"""

import io
import sys
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ROOT = Path(__file__).resolve().parents[1]
LOGO = ROOT / "src/mediareaderx/assets/logo.svg"

# 3 x 3 grid snapped to whole pixels at 16 and 32 px: 64-unit pixels, corner to corner, 32-unit margin.
_PIXEL = '<rect x="{x}" y="{y}" width="64" height="64" rx="6" fill="{fill}"/>'
SMALL = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">'
         '<rect width="256" height="256" rx="56" fill="#1C2540"/>'
         + "".join(_PIXEL.format(x=x, y=y, fill=fill) for x, y, fill in (
             (32, 32, "#3CC8B4"), (160, 160, "#3CC8B4"), (160, 32, "#FF7A59"), (32, 160, "#FF7A59"),
             (96, 96, "#FFF6EC")))
         + "</svg>")


def render(svg: bytes, size: int) -> Image.Image:
    renderer = QSvgRenderer(QByteArray(svg))
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return Image.open(io.BytesIO(bytes(buf.data()))).convert("RGBA")


def build(out: Path) -> None:
    full = LOGO.read_bytes()
    small = SMALL.encode()
    sources = {256: full, 128: full, 64: full, 48: full, 32: small, 24: small, 16: small}
    images = {size: render(svg, size) for size, svg in sources.items()}
    out.parent.mkdir(parents=True, exist_ok=True)
    # Pillow writes one entry per size, each from the closest provided frame.
    images[256].save(out, sizes=[(s, s) for s in sources], append_images=list(images.values()))
    images[256].save(out.with_name("logo-256.png"))


if __name__ == "__main__":
    _app = QGuiApplication.instance() or QGuiApplication(["make_icon", "-platform", "offscreen"])
    target = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "src/mediareaderx/assets/app.ico")
    build(target)
    print("icon written to", target.resolve())
