"""Toolbar line icons, drawn as inline SVG and tinted to the current theme.

Each icon is a 24 x 24 stroke drawing. ``icon(name, color)`` renders it at 2x
for sharp results on high-DPI screens.
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

_PATHS = {
    "open": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "prev": '<path d="M15 5l-7 7 7 7"/>',
    "next": '<path d="M9 5l7 7-7 7"/>',
    "zoom_in": '<circle cx="11" cy="11" r="7"/><path d="M16.5 16.5L21 21M8 11h6M11 8v6"/>',
    "zoom_out": '<circle cx="11" cy="11" r="7"/><path d="M16.5 16.5L21 21M8 11h6"/>',
    "fit": '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
    "rotate_left": '<path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 3v6h6"/>',
    "rotate_right": '<path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 3v6h-6"/>',
    "live": '<circle cx="12" cy="12" r="3"/><circle cx="12" cy="12" r="6.5"/>'
            '<circle cx="12" cy="12" r="10" stroke-dasharray="2 2.6"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.01"/>',
    "strip": '<rect x="3" y="6" width="18" height="12" rx="2"/><path d="M9 6v12M15 6v12"/>',
    "loop": '<path d="M17 2l3 3-3 3"/><path d="M4 11V9a4 4 0 0 1 4-4h12"/>'
            '<path d="M7 22l-3-3 3-3"/><path d="M20 13v2a4 4 0 0 1-4 4H4"/>',
}

NAMES = tuple(_PATHS)


def _svg(name: str, color: str) -> bytes:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
            f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{_PATHS[name]}</svg>').encode()


def icon(name: str, color: QColor | str) -> QIcon:
    hex_color = QColor(color).name()
    renderer = QSvgRenderer(QByteArray(_svg(name, hex_color)))
    result = QIcon()
    for size in (24, 48):
        img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        painter = QPainter(img)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter)
        painter.end()
        result.addPixmap(QPixmap.fromImage(img))
    return result
