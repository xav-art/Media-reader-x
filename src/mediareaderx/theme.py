"""Light, dark, or follow-Windows theme, with a dark palette tinted to the brand ink.

Brand tokens come from the "Pixel x" icon. Dark surfaces use the ink hue
instead of Fusion's neutral grey, so the window and the icon feel like one product.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

THEMES = {"system": "Same as Windows", "light": "Light", "dark": "Dark"}
_SCHEMES = {"system": Qt.ColorScheme.Unknown, "light": Qt.ColorScheme.Light, "dark": Qt.ColorScheme.Dark}

# Brand tokens (see DESIGN.md).
INK = "#1C2540"
CORAL = "#FF7A59"
TEAL = "#3CC8B4"
IVORY = "#FFF6EC"

# Dark theme surfaces: base < window < raised, all tinted toward INK.
_DARK = {
    QPalette.ColorRole.Window: "#171E31",
    QPalette.ColorRole.Base: "#11172A",
    QPalette.ColorRole.AlternateBase: "#1B2338",
    QPalette.ColorRole.Button: "#212A42",
    QPalette.ColorRole.Light: "#2E3956",
    QPalette.ColorRole.Midlight: "#262F4A",
    QPalette.ColorRole.Mid: "#3A4560",
    QPalette.ColorRole.Dark: "#0D1222",
    QPalette.ColorRole.Shadow: "#070A14",
    QPalette.ColorRole.Text: "#E8ECF4",
    QPalette.ColorRole.WindowText: "#E8ECF4",
    QPalette.ColorRole.ButtonText: "#E8ECF4",
    QPalette.ColorRole.BrightText: "#FFFFFF",
    QPalette.ColorRole.PlaceholderText: "#8B95AB",
    QPalette.ColorRole.Highlight: CORAL,
    QPalette.ColorRole.HighlightedText: INK,  # 6:1 on coral
    QPalette.ColorRole.Link: "#FF9A80",
    QPalette.ColorRole.ToolTipBase: "#212A42",
    QPalette.ColorRole.ToolTipText: "#E8ECF4",
}
_DARK_DISABLED_TEXT = "#6B7590"


def dark_palette() -> QPalette:
    pal = QPalette()
    for role, color in _DARK.items():
        pal.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(_DARK_DISABLED_TEXT))
    return pal


def is_dark(name: str) -> bool:
    if name == "dark":
        return True
    if name == "light":
        return False
    return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark


def apply_theme(name: str) -> str:
    """Apply ``name`` (system/light/dark) and return the name actually used."""
    name = name if name in _SCHEMES else "system"
    # Unknown hands control back to Windows, which also tracks later changes.
    QGuiApplication.styleHints().setColorScheme(_SCHEMES[name])
    app = QApplication.instance()
    if app is not None:
        app.setPalette(dark_palette() if is_dark(name) else app.style().standardPalette())
    return name
