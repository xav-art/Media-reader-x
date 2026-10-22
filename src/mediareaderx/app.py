"""Application bootstrap: CLI arguments, logging, crash handling, main window."""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import traceback

from mediareaderx import APP_ID, APP_NAME, ORG_NAME, __version__

log = logging.getLogger("mediareaderx")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="MediaReaderX", description=f"{APP_NAME}: offline media viewer.")
    parser.add_argument("file", nargs="?", help="image or video to open")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="log verbosity (default: INFO)")
    parser.add_argument("--self-test", action="store_true",
                        help="check every bundled codec without opening a window; exit code 0 = healthy")
    # Qt consumes its own options (e.g. -platform); ignore anything unknown.
    args, _unknown = parser.parse_known_args(argv)
    return args


def _install_excepthook() -> None:
    from PySide6.QtWidgets import QApplication, QMessageBox

    def hook(exc_type, exc, tb):
        log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))
        # Widgets may only be created on the GUI thread: from a worker thread, log only.
        if QApplication.instance() is not None and threading.current_thread() is threading.main_thread():
            box = QMessageBox(QMessageBox.Icon.Critical, APP_NAME,
                              "An unexpected error occurred. The details were saved to the log "
                              "(Help > Open log folder).")
            box.setDetailedText("".join(traceback.format_exception(exc_type, exc, tb)))
            box.exec()

    sys.excepthook = hook


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    from PySide6.QtCore import QCoreApplication
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from mediareaderx.logging_setup import setup_logging

    setup_logging(args.log_level)
    if args.self_test:
        from mediareaderx.selftest import run_self_test

        return run_self_test()
    _install_excepthook()
    from mediareaderx.config import migrate_legacy_settings

    if migrate_legacy_settings():
        log.info("Imported settings from Media Reader 1.0")
    QCoreApplication.setOrganizationName(ORG_NAME)
    QCoreApplication.setApplicationName(APP_ID)
    QCoreApplication.setApplicationVersion(__version__)

    app = QApplication(sys.argv[:1])
    app.setApplicationDisplayName(APP_NAME)
    app.setStyle("Fusion")
    from mediareaderx.resources import icon_path

    icon = icon_path()
    if icon:
        app.setWindowIcon(QIcon(str(icon)))

    from mediareaderx.main_window import MainWindow

    window = MainWindow()
    window.show()
    if args.file:
        window.open_path(args.file)
    else:
        # The welcome screen is idle time: load the codecs now, so the first photo opens at full speed.
        from mediareaderx.decoders import warm_up

        threading.Thread(target=warm_up, name="codec-warm-up", daemon=True).start()
    code = app.exec()
    log.info("Exiting with code %s", code)
    return code
