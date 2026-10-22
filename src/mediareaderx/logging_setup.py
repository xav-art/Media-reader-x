"""Rotating local log file plus capture of Qt and uncaught Python errors.

Logs stay on the user's machine. File names are logged by extension and size
only, never by full path, so logs can be attached to a support ticket safely.
"""

from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QtMsgType, qInstallMessageHandler

from mediareaderx import __version__
from mediareaderx.config import log_dir

log = logging.getLogger("mediareaderx")

_QT_LEVELS = {
    QtMsgType.QtDebugMsg: logging.DEBUG,
    QtMsgType.QtInfoMsg: logging.INFO,
    QtMsgType.QtWarningMsg: logging.WARNING,
    QtMsgType.QtCriticalMsg: logging.ERROR,
    QtMsgType.QtFatalMsg: logging.CRITICAL,
}


def describe_file(path: str | Path) -> str:
    """Privacy-safe description of a file for log lines."""
    p = Path(path)
    try:
        size = f"{p.stat().st_size / 1024:.0f} KB"
    except OSError:
        size = "size unknown"
    return f"{p.suffix.lower() or '<no ext>'} ({size})"


def setup_logging(level: str = "INFO") -> Path:
    log_file = log_dir() / "mediareaderx.log"
    handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(handler)
    if sys.stderr is not None:  # None in a windowed (no console) build
        root.addHandler(logging.StreamHandler(sys.stderr))

    def qt_handler(mode, _context, message):
        logging.getLogger("qt").log(_QT_LEVELS.get(mode, logging.INFO), message)

    qInstallMessageHandler(qt_handler)
    threading.excepthook = lambda a: log.critical(
        "Unhandled thread exception", exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    log.info("Media Reader x %s starting (Python %s)", __version__, sys.version.split()[0])
    return log_file
