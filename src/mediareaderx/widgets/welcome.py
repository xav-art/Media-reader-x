"""Start screen: what the app does, the two ways in, and recent files."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

from PySide6.QtCore import QByteArray, QSize, Qt, Signal
from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from mediareaderx import APP_NAME
from mediareaderx.resources import resource_dir
from mediareaderx.theme import CORAL, INK

MAX_RECENT_SHOWN = 6
LOGO_PX = 96

# The one primary action uses the brand accent; ink text on coral is 6:1.
PRIMARY_STYLE = f"""
QPushButton#primary {{ background: {CORAL}; color: {INK}; border: 2px solid {CORAL};
                       border-radius: 6px; padding: 8px 20px; font-weight: 600; }}
QPushButton#primary:hover {{ background: #FF8E72; border-color: #FF8E72; }}
QPushButton#primary:focus {{ border: 2px solid palette(text); }}
"""


_DRIVE_FIXED, _DRIVE_RAMDISK = 3, 6


def is_local_path(path: str) -> bool:
    """True for a fixed or RAM disk drive letter. UNC paths and mapped network drives are not
    local. GetDriveTypeW answers from local state, without touching the network."""
    if path.startswith(("\\\\", "//")):
        return False
    drive = os.path.splitdrive(path)[0]
    if not drive:
        return True  # relative path: local by definition
    try:
        return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") in (_DRIVE_FIXED, _DRIVE_RAMDISK)
    except (AttributeError, OSError):  # not Windows
        return True


def _logo_pixmap(size: int, dpr: float) -> QPixmap:
    path = resource_dir() / "logo.svg"
    px = int(size * dpr)
    img = QImage(px, px, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    if path.is_file():
        painter = QPainter(img)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        QSvgRenderer(QByteArray(path.read_bytes())).render(painter)
        painter.end()
    pixmap = QPixmap.fromImage(img)
    pixmap.setDevicePixelRatio(dpr)
    return pixmap


class WelcomeView(QWidget):
    openFileRequested = Signal()
    openFolderRequested = Signal()
    recentRequested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(PRIMARY_STYLE)
        self.logo = QLabel(self)
        self.logo.setFixedSize(QSize(LOGO_PX, LOGO_PX))
        self.logo.setAccessibleName(f"{APP_NAME} logo")

        title = QLabel(APP_NAME, self)
        font = title.font()
        font.setPointSizeF(font.pointSizeF() * 1.8)
        font.setBold(True)
        title.setFont(font)
        intro = QLabel("Open a photo or a video. You can also drag files or a folder into this window.", self)
        intro.setWordWrap(True)
        intro.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.open_file_btn = QPushButton("Open file", self)
        self.open_file_btn.setObjectName("primary")
        self.open_file_btn.setToolTip("Open file (Ctrl+O)")
        self.open_file_btn.setDefault(True)
        self.open_file_btn.clicked.connect(self.openFileRequested)
        self.open_folder_btn = QPushButton("Open folder", self)
        self.open_folder_btn.setToolTip("Open folder (Ctrl+Shift+O)")
        self.open_folder_btn.clicked.connect(self.openFolderRequested)
        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        buttons.addStretch(1)
        buttons.addWidget(self.open_file_btn)
        buttons.addWidget(self.open_folder_btn)
        buttons.addStretch(1)

        self.recent_title = QLabel("Recent files", self)
        bold = self.recent_title.font()
        bold.setBold(True)
        self.recent_title.setFont(bold)
        self.recent_box = QVBoxLayout()
        self.recent_box.setSpacing(2)

        column = QVBoxLayout()
        column.setSpacing(16)
        column.addWidget(self.logo, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addWidget(title, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addWidget(intro)
        column.addLayout(buttons)
        column.addSpacing(16)
        column.addWidget(self.recent_title)
        column.addLayout(self.recent_box)

        holder = QWidget(self)
        holder.setLayout(column)
        holder.setMaximumWidth(520)
        outer = QVBoxLayout(self)
        outer.addStretch(1)
        outer.addWidget(holder, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(2)
        self.recent_buttons: list[QPushButton] = []

    def showEvent(self, event) -> None:
        self.logo.setPixmap(_logo_pixmap(LOGO_PX, self.devicePixelRatioF()))
        super().showEvent(event)

    def set_recent(self, paths: list[str]) -> None:
        for btn in self.recent_buttons:
            self.recent_box.removeWidget(btn)
            btn.deleteLater()
        self.recent_buttons = []
        # Checking a network path can block for seconds (VPN off, dead share), so only local
        # files are checked here. A missing remote file shows "cannot be found" when clicked.
        existing = [p for p in paths if not is_local_path(p) or Path(p).exists()][:MAX_RECENT_SHOWN]
        for path in existing:
            p = Path(path)
            btn = QPushButton(f"{p.name}    {p.parent.name}", self)
            btn.setFlat(True)
            # Enough vertical padding that underscores in names (IMG_1234) are not clipped.
            btn.setStyleSheet("text-align: left; padding: 6px 8px 8px 8px;")
            btn.setMinimumHeight(btn.fontMetrics().height() + 16)
            btn.setToolTip(path)
            btn.setAccessibleName(f"Open recent file {p.name}")
            btn.clicked.connect(lambda _=False, x=path: self.recentRequested.emit(x))
            self.recent_box.addWidget(btn)
            self.recent_buttons.append(btn)
        self.recent_title.setVisible(bool(existing))
