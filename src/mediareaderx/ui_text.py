"""Show text from files as text, never as Qt rich text.

Qt guesses: a label, tooltip or message box whose text looks like HTML is rendered as HTML, and Qt's HTML
loads pictures from ``data:`` URLs, files and network shares (``file://server/share/x.png``, an SMB
connection that can leak the Windows password hash). Metadata, file names and error messages come from
files, so every place that shows them goes through this module (a test enforces it).
"""

from __future__ import annotations

import html

from PySide6.QtCore import Qt


def escaped(text: str) -> str:
    """``text`` as explicit rich text that shows exactly the characters given (line breaks kept)."""
    return "<p style='white-space:pre-wrap'>" + html.escape(str(text), quote=False) + "</p>"


def tooltip(text: str) -> str:
    """A tooltip that shows ``text`` literally (tooltips cannot be set to plain text)."""
    return escaped(text)


def box_text(text: str) -> str:
    """Text for a QMessageBox static function (those guess the format): shown literally."""
    return escaped(text)


def plain(*labels) -> None:
    """Set labels to plain text: they never interpret what they show."""
    for label in labels:
        label.setTextFormat(Qt.TextFormat.PlainText)
