"""Rotating local log file plus capture of Qt and uncaught Python errors.

Logs stay on the user's machine. Files are logged by extension and size only. Every line also passes
through ``redact``, so a path or file name in an error message, a traceback or a Qt warning never
reaches the log either: logs can be attached to a support ticket safely.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QtMsgType, qInstallMessageHandler

from mediareaderx import __version__
from mediareaderx.config import log_dir
from mediareaderx.formats import ALL_EXTS

log = logging.getLogger("mediareaderx")

_QT_LEVELS = {
    QtMsgType.QtDebugMsg: logging.DEBUG,
    QtMsgType.QtInfoMsg: logging.INFO,
    QtMsgType.QtWarningMsg: logging.WARNING,
    QtMsgType.QtCriticalMsg: logging.ERROR,
    QtMsgType.QtFatalMsg: logging.CRITICAL,
}


def describe_file(path: str | Path) -> str:
    """Privacy-safe description of a file for log lines: a known media extension and the size, nothing else.
    (A name like "Letter to Dr. Hansen" has the "extension" ". hansen", so only known extensions are logged.)"""
    p = Path(path)
    try:
        size = f"{p.stat().st_size / 1024:.0f} KB"
    except OSError:
        size = "size unknown"
    ext = p.suffix.lower()
    return f"{ext if ext in ALL_EXTS else '<other type>'} ({size})"


_KEEP_NAME = (".py", ".pyd", ".dll")  # program files in a traceback: their name helps, their folder is private
# A quoted path: 'C:\x', "D:/x", '\\server\share', '/x/y', 'file:///x'.
_QUOTED_PATH = re.compile(r"""(['"])((?:[A-Za-z]:|\\\\|\\|/|file:)[^'"\n]*)\1""")
# An unquoted path runs to the end of the line: a name can contain spaces, so there is no safe shorter end.
_BARE_PATH = re.compile(r"(?:(?<![\w])(?:[A-Za-z]:[\\/]|\\\\[^\\\s]|file:/)|(?<![\w:/])//[^/\s]+/)[^\n]*")


def redact(text: str) -> str:
    """Remove paths and file names from a log line (see the module docstring)."""
    def quoted(m: re.Match) -> str:
        value = m.group(2)
        name = re.split(r"[\\/]+", value)[-1]
        keep = name if name.lower().endswith(_KEEP_NAME) else "<path>"
        return f"{m.group(1)}{keep}{m.group(1)}"

    text = _QUOTED_PATH.sub(quoted, text)
    text = _BARE_PATH.sub("<path>", text)
    home = os.path.expanduser("~")
    if len(home) > 3:  # the user name, wherever it is left (for example in a URL)
        for form in {home, home.replace("\\", "/"), home.replace("\\", "\\\\")}:
            text = text.replace(form, "~")
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(level: str = "INFO") -> Path:
    log_file = log_dir() / "mediareaderx.log"
    handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(handler)
    if sys.stderr is not None:  # None in a windowed (no console) build
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(RedactingFormatter())
        root.addHandler(console)

    def qt_handler(mode, _context, message):
        logging.getLogger("qt").log(_QT_LEVELS.get(mode, logging.INFO), message)

    qInstallMessageHandler(qt_handler)
    threading.excepthook = lambda a: log.critical(
        "Unhandled thread exception", exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    log.info("Media Reader x %s starting (Python %s)", __version__, sys.version.split()[0])
    return log_file
