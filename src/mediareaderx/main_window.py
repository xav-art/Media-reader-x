"""Main window: routes files to the image canvas or the video player."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from PySide6.QtCore import (QByteArray, QEvent, QFile, QMimeData, QObject, QPoint, QRunnable, QSettings, QSize,
                            Qt, QThreadPool, QTimer, QUrl, Signal)
from PySide6.QtGui import (QAction, QActionGroup, QBrush, QCloseEvent, QColor, QDragEnterEvent, QDropEvent,
                           QGuiApplication, QImage, QKeySequence, QPalette, QShortcut)
from PySide6.QtWidgets import (QApplication, QFileDialog, QLabel, QMainWindow, QMessageBox, QStackedWidget,
                               QToolBar)

from mediareaderx import APP_NAME, __version__, icons
from mediareaderx.cache import DecodedCache, budget_for_screen, cache_key
from mediareaderx.config import log_dir, settings_file
from mediareaderx.decoders import DecodedImage, DecodeError, decode
from mediareaderx.export import ExportError, export_image
from mediareaderx.folder import FolderNav, list_media
from mediareaderx.formats import IMAGE_EXTS, RAW_EXTS, SVG_EXTS, VIDEO_EXTS, MediaKind, dialog_filter, kind_of
from mediareaderx.logging_setup import describe_file
from mediareaderx.metadata import file_items
from mediareaderx.theme import THEMES, apply_theme
from mediareaderx.widgets.filmstrip import Filmstrip
from mediareaderx.widgets.image_view import ImageView
from mediareaderx.widgets.metadata_panel import MetadataPanel
from mediareaderx.widgets.print_dialog import PrintDialog
from mediareaderx.widgets.video_view import VideoView
from mediareaderx.widgets.welcome import WelcomeView

log = logging.getLogger(__name__)

MAX_RECENT = 10
META_PANEL_PX = 300  # the photo stays the focal point
LAYOUT_VERSION = 2
PRELOAD_OFFSETS = (1, -1, 2)  # next first: people mostly move forward
IDLE_HIDE_MS = 2500  # full screen: hide cursor and video controls after this idle time
OVERLAY_MS = 2000
SLIDESHOW_SECONDS = (3, 5, 10)
PRIORITY_CURRENT, PRIORITY_PRELOAD = 10, 0
NO_PRELOAD_EXTS = frozenset({".gif", ".apng"})  # animations can be hundreds of MB: decode only on demand
FALLBACK_SCREEN_SIDE = 2560


def _printable(path: Path) -> bool:
    return kind_of(path) not in (None, MediaKind.VIDEO)


def screen_side() -> int:
    """The longest side, in device pixels, of the largest screen: the size of a screen copy."""
    sides = [round(max(s.size().width(), s.size().height()) * s.devicePixelRatio())
             for s in QGuiApplication.screens()]
    return max(sides, default=0) or FALLBACK_SCREEN_SIDE


class _DecodeSignals(QObject):
    # path, DecodedImage or None, cache key, error or None, True for a full-resolution decode
    finished = Signal(str, object, object, object, bool)


class _DecodeTask(QRunnable):
    """Decode one file on the pool: the open file, a neighbour ahead of time, or (``max_side``
    None) the open file at full resolution for zoom.

    Every outcome is reported through ``signals.finished`` (never an escaping exception),
    so the window always leaves "Loading" and bookkeeping is always cleared.
    """

    def __init__(self, path: Path, signals: _DecodeSignals, max_side: int | None):
        super().__init__()
        self.path, self.signals, self.max_side = path, signals, max_side
        self.started = False  # read on the GUI thread to tell queued tasks from running ones

    def run(self) -> None:
        self.started = True
        key = cache_key(self.path)  # before decoding: see cache.cache_key
        image, message = None, None
        try:
            image = decode(self.path, self.max_side)
        except DecodeError as exc:
            message = str(exc)
        except Exception:  # decode() maps known errors; anything else is a bug, never a hang
            log.exception("Decode task failed")
            message = f"{APP_NAME} cannot open this image. To see why, select Help > Open log folder."
        try:
            self.signals.finished.emit(str(self.path), image, key, message, self.max_side is None)
        except RuntimeError:
            pass  # the window closed while this file decoded


class MainWindow(QMainWindow):
    PAGE_WELCOME, PAGE_IMAGE, PAGE_VIDEO, PAGE_MESSAGE = range(4)

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setAcceptDrops(True)
        self.settings = QSettings(str(settings_file()), QSettings.Format.IniFormat)
        self.theme = apply_theme(str(self.settings.value("theme", "system")))
        self._applying_theme = False
        self._icon_actions: dict[str, QAction] = {}
        QGuiApplication.styleHints().colorSchemeChanged.connect(self._on_scheme_changed)
        self.current: Path | None = None
        self.companion: Path | None = None
        self.nav = FolderNav()
        self._token = 0
        self._current_image: DecodedImage | None = None
        # Photos are decoded and preloaded as screen copies (about 10x less memory at 48 MP).
        # Full resolution is decoded for the open photo only, when zoom, copy or save needs it.
        self._max_side = screen_side()
        self._full_image: DecodedImage | None = None
        self._full_wanted: tuple[int, str] | None = None  # (token, path) of a running full decode
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        # The open photo, its preloaded neighbours, and one more to step back without a decode.
        self._cache = DecodedCache(budget_for_screen(self._max_side, len(PRELOAD_OFFSETS) + 2))
        # Queued or running decodes by path. One signals object, owned by the window, for all of them.
        self._inflight: dict[str, _DecodeTask] = {}
        self._awaiting: tuple[int, str] | None = None  # (token, path) the screen is waiting for
        self._decode_signals = _DecodeSignals(self)
        self._decode_signals.finished.connect(self._on_task_finished)

        self._slide_timer = QTimer(self)
        self._slide_timer.setSingleShot(True)
        self._slide_timer.timeout.connect(self._slideshow_next)
        self._slideshow_entered_fullscreen = False
        self._saved_layout: QByteArray | None = None
        self._saved_window_state = Qt.WindowState.WindowNoState
        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.setInterval(IDLE_HIDE_MS)
        self._idle_timer.timeout.connect(self._on_idle)
        self._cursor_hidden = False
        self._docks_before_welcome: tuple[bool, bool] | None = None

        self.stack = QStackedWidget(self)
        self.welcome = WelcomeView(self)
        self.welcome.openFileRequested.connect(self.open_dialog)
        self.welcome.openFolderRequested.connect(self.open_folder_dialog)
        self.welcome.recentRequested.connect(self.open_path)
        self.image_view = ImageView(self)
        self.video_view = VideoView(self)
        self.message = self._message_label("")
        for w in (self.welcome, self.image_view, self.video_view, self.message):
            self.stack.addWidget(w)
        self.setCentralWidget(self.stack)
        # Full-screen overlay: file name and position, shown briefly after each move. It is a
        # borderless tool window, not a child widget: Windows draws video on a native surface
        # that would cover a child widget (review R18, confirmed in the packaged app).
        self.overlay = QLabel(self, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                              | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.overlay.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.overlay.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.overlay.setStyleSheet("background: rgba(12, 17, 32, 200); color: white; padding: 8px 12px;"
                                   " border-radius: 6px;")
        self.overlay.hide()
        self._overlay_timer = QTimer(self)
        self._overlay_timer.setSingleShot(True)
        self._overlay_timer.timeout.connect(self.overlay.hide)

        self.meta_panel = MetadataPanel(self)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.meta_panel)
        self.filmstrip = Filmstrip(self)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.filmstrip)
        self.filmstrip.fileActivated.connect(self.open_path)
        self.status = QLabel(self)
        self.statusBar().addWidget(self.status, 1)

        self.image_view.zoomChanged.connect(lambda _z: self._update_status())
        self.image_view.detailNeeded.connect(self._request_full)
        self.video_view.metadataReady.connect(self._on_video_metadata)
        self.video_view.failed.connect(self._on_video_failed)
        self.video_view.loop_btn.toggled.connect(lambda on: self.settings.setValue("video_loop", on))
        self.video_view.ended.connect(self._on_video_ended)
        self._build_actions()
        self._restore_settings()
        self._show_welcome()
        QApplication.instance().installEventFilter(self)  # mouse buttons and full-screen activity

    # -- UI construction -----------------------------------------------------
    def _message_label(self, text: str) -> QLabel:
        label = QLabel(text, self)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        label.setMargin(32)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    def _action(self, text: str, slot, shortcuts=(), checkable=False, icon_name: str | None = None) -> QAction:
        act = QAction(text, self)
        act.setShortcuts([QKeySequence(s) for s in shortcuts])
        act.setCheckable(checkable)
        act.triggered.connect(slot)
        self._set_tooltip(act)
        if icon_name:
            self._icon_actions[icon_name] = act
        self.addAction(act)
        return act

    @staticmethod
    def _set_tooltip(act: QAction) -> None:
        """Toolbar buttons are icon-only, so the tooltip carries the name and the key."""
        label = act.text().replace("&", "").removesuffix("...")
        key = act.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
        act.setToolTip(f"{label} ({key})" if key else label)

    def changeEvent(self, event) -> None:
        # Windows applies a colour scheme asynchronously, so re-tint on every palette change.
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ThemeChange) and self._icon_actions:
            self._apply_icons()
        super().changeEvent(event)

    def _apply_icons(self) -> None:
        color = QApplication.palette().color(QPalette.ColorRole.ButtonText)
        for name, act in self._icon_actions.items():
            act.setIcon(icons.icon(name, color))
        self.video_view.set_icon_color(color)

    def _on_scheme_changed(self, _scheme) -> None:
        """Windows switched light/dark: follow it when the theme is "Same as Windows"."""
        if self.theme == "system" and not self._applying_theme:
            self._applying_theme = True
            apply_theme("system")
            self._applying_theme = False
        self._apply_icons()

    def _build_actions(self) -> None:
        a = self._action
        self.act_open = a("&Open...", self.open_dialog, [QKeySequence.StandardKey.Open], icon_name="open")
        self.act_open_folder = a("Open &folder...", self.open_folder_dialog, ["Ctrl+Shift+O"])
        self.act_slideshow = a("&Slideshow", self.toggle_slideshow, ["F5"], checkable=True)
        self.act_save = a("&Save a copy as JPG/PNG...", self.save_copy, ["Ctrl+S"])
        self.act_print = a("&Print...", self.print_dialog, [QKeySequence.StandardKey.Print])
        self.act_strip_gps = a("Remove location (GPS) when saving", lambda on: self.settings.setValue(
            "strip_gps", on), checkable=True)
        self.act_delete = a("Move to Recycle &Bin", self.delete_current, ["Del"])
        self.act_reveal = a("Show in &Explorer", self.reveal_in_explorer, ["Ctrl+Shift+E"])
        self.act_exit = a("E&xit", self.close, [QKeySequence.StandardKey.Quit])
        self.act_copy = a("&Copy picture", self.copy, [QKeySequence.StandardKey.Copy])
        self.act_copy_path = a("Copy file &path", self.copy_path, ["Ctrl+Shift+C"])
        self.act_prev = a("&Previous file", lambda: self._go(self.nav.step(-1)), ["Left", "PgUp"], icon_name="prev")
        self.act_next = a("&Next file", lambda: self._go(self.nav.step(1)), ["Right", "PgDown"], icon_name="next")
        self.act_first = a("&First file", lambda: self._go(self.nav.first()), ["Home"])
        self.act_last = a("&Last file", lambda: self._go(self.nav.last()), ["End"])
        self.act_zoom_in = a("Zoom &in", lambda: self.image_view.zoom_by(1.25), ["+", "Ctrl++", "="],
                             icon_name="zoom_in")
        self.act_zoom_out = a("Zoom &out", lambda: self.image_view.zoom_by(0.8), ["-", "Ctrl+-"],
                              icon_name="zoom_out")
        self.act_fit = a("&Fit to window", self.image_view.fit_to_window, ["F", "Ctrl+0"], icon_name="fit")
        self.act_actual = a("&Actual size (100%)", self.image_view.actual_size, ["1", "Ctrl+1"])
        self.act_rot_right = a("Rotate &right", lambda: self._rotate(90), ["R"], icon_name="rotate_right")
        self.act_rot_left = a("Rotate le&ft", lambda: self._rotate(-90), ["Shift+R"], icon_name="rotate_left")
        self.act_full = a("F&ull screen", self.toggle_fullscreen, ["F11"], checkable=True)
        self.act_live = a("Play &Live Photo", self.toggle_live_photo, ["L"], checkable=True, icon_name="live")
        self.act_loop = a("&Loop video", self.video_view.loop_btn.toggle, ["Ctrl+L"])
        self.act_slower = a("&Slower", lambda: self.video_view.change_speed(-1), ["["])
        self.act_faster = a("F&aster", lambda: self.video_view.change_speed(1), ["]"])
        self.act_meta = self.meta_panel.toggleViewAction()
        self.act_meta.setText("&Metadata panel")
        self.act_meta.setShortcut("I")
        self._set_tooltip(self.act_meta)
        self._icon_actions["info"] = self.act_meta
        self.addAction(self.act_meta)
        self.act_strip = self.filmstrip.toggleViewAction()
        self.act_strip.setText("&Thumbnail strip")
        self.act_strip.setShortcut("T")
        self._set_tooltip(self.act_strip)
        self._icon_actions["strip"] = self.act_strip
        self.addAction(self.act_strip)

        bar = self.menuBar()
        file_menu = bar.addMenu("&File")
        file_menu.addActions([self.act_open, self.act_open_folder])
        self.recent_menu = file_menu.addMenu("Open &recent")
        self.recent_menu.aboutToShow.connect(self._fill_recent_menu)
        file_menu.addSeparator()
        file_menu.addActions([self.act_save, self.act_strip_gps])
        file_menu.addAction(self.act_print)
        file_menu.addSeparator()
        file_menu.addActions([self.act_delete, self.act_reveal])
        file_menu.addSeparator()
        file_menu.addAction(self.act_exit)
        edit = bar.addMenu("&Edit")
        edit.addActions([self.act_copy, self.act_copy_path])
        view = bar.addMenu("&View")
        view.addActions([self.act_prev, self.act_next, self.act_first, self.act_last])
        view.addSeparator()
        view.addActions([self.act_zoom_in, self.act_zoom_out, self.act_fit, self.act_actual,
                         self.act_rot_left, self.act_rot_right])
        view.addSeparator()
        view.addActions([self.act_live, self.act_meta, self.act_strip, self.act_full, self.act_slideshow])
        interval_menu = view.addMenu("Slideshow &interval")
        interval_group = QActionGroup(self)
        current_interval = self.slideshow_seconds
        for seconds in SLIDESHOW_SECONDS:
            act = interval_menu.addAction(f"{seconds} seconds", lambda s=seconds: self.settings.setValue(
                "slideshow_seconds", s))
            act.setCheckable(True)
            act.setChecked(seconds == current_interval)
            interval_group.addAction(act)
        theme_menu = view.addMenu("T&heme")
        group = QActionGroup(self)
        for key, label in THEMES.items():
            act = theme_menu.addAction(label, lambda k=key: self.set_theme(k))
            act.setCheckable(True)
            act.setChecked(key == self.theme)
            group.addAction(act)
        video = bar.addMenu("&Video")
        video.addActions([self.act_slower, self.act_faster, self.act_loop])
        help_menu = bar.addMenu("&Help")
        help_menu.addAction("&Keyboard shortcuts", self.show_shortcuts)
        help_menu.addAction("Supported &formats", self.show_formats)
        help_menu.addAction("Open &log folder", lambda: os.startfile(log_dir()))
        help_menu.addAction(f"&About {APP_NAME}", self.show_about)

        tb = self.addToolBar("Main")
        tb.setObjectName("MainToolbar")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        tb.setIconSize(QSize(20, 20))
        tb.addActions([self.act_open, self.act_prev, self.act_next])
        tb.addSeparator()
        tb.addActions([self.act_zoom_out, self.act_zoom_in, self.act_fit, self.act_rot_left, self.act_rot_right])
        tb.addSeparator()
        tb.addActions([self.act_live, self.act_meta, self.act_strip])

        # Space plays/pauses a video, or moves to the next photo.
        QShortcut(QKeySequence("Space"), self, self._on_space)
        for keys, fn in ((("K",), self.video_view.toggle_play),
                         (("Shift+Right",), lambda: self.video_view.step(5000)),
                         (("Shift+Left",), lambda: self.video_view.step(-5000)),
                         (("M",), self.video_view.toggle_mute)):
            for key in keys:
                QShortcut(QKeySequence(key), self, lambda f=fn: self._if_video(f))
        QShortcut(QKeySequence("Escape"), self, self._on_escape)
        self._apply_icons()
        self._sync_actions()

    def _if_video(self, fn) -> None:
        if self.stack.currentIndex() == self.PAGE_VIDEO:
            fn()

    def _on_space(self) -> None:
        if self.stack.currentIndex() == self.PAGE_VIDEO:
            self.video_view.toggle_play()
        else:
            self._go(self.nav.step(1))

    def _sync_actions(self) -> None:
        page = self.stack.currentIndex()
        is_image = page == self.PAGE_IMAGE and self._current_image is not None
        is_video = page == self.PAGE_VIDEO
        for act in (self.act_zoom_in, self.act_zoom_out, self.act_fit, self.act_actual,
                    self.act_rot_left, self.act_rot_right):
            act.setEnabled(is_image)
            act.setVisible(is_image)  # image-only tools leave the toolbar for video
        self.act_save.setEnabled(is_image)
        self.act_print.setEnabled(any(_printable(p) for p in self.nav.files))
        for act in (self.act_loop, self.act_slower, self.act_faster):
            act.setEnabled(is_video)
        self.act_copy.setEnabled(is_image or (self.current is not None and self.current.is_file()))
        has_file = self.current is not None
        for act in (self.act_reveal, self.act_copy_path, self.act_delete):
            act.setEnabled(has_file)
        self.act_prev.setEnabled(self.nav.index > 0)
        self.act_next.setEnabled(0 <= self.nav.index < len(self.nav.files) - 1)
        self.act_first.setEnabled(self.act_prev.isEnabled())
        self.act_last.setEnabled(self.act_next.isEnabled())
        self.act_live.setEnabled(self.companion is not None)
        self.act_live.setVisible(self.companion is not None)

    # -- opening files -------------------------------------------------------
    def open_dialog(self) -> None:
        start = self.settings.value("last_dir", str(Path.home() / "Pictures"))
        path, _ = QFileDialog.getOpenFileName(self, "Open media", start, dialog_filter())
        if path:
            self.open_path(path)

    def open_folder_dialog(self) -> None:
        start = self.settings.value("last_dir", str(Path.home() / "Pictures"))
        folder = QFileDialog.getExistingDirectory(self, "Open folder", start)
        if folder:
            self.open_folder(folder)

    def open_folder(self, folder: str | Path) -> None:
        """Show the first supported file of ``folder``, in File Explorer order."""
        folder = Path(folder)
        files = list_media(folder)
        if files:
            self.open_path(files[0])
            return
        self.current = None
        self.nav = FolderNav()
        self.filmstrip.set_files([], -1)
        self.setWindowTitle(f"{folder.name} - {APP_NAME}")
        self._show_message(f"The folder \"{folder.name}\" has no photos or videos that {APP_NAME} can open.")

    def _go(self, path: Path | None) -> None:
        if path is not None:
            self.open_path(path)

    def open_path(self, path: str | Path) -> None:
        path = Path(path)
        if path.is_dir():
            self.open_folder(path)
            return
        if not self.immersive:
            self._leave_welcome()
        kind = kind_of(path)
        log.info("Opening %s as %s", describe_file(path), kind.value if kind else "unknown")
        self._token += 1
        self.video_view.stop()
        self.image_view.clear()
        self.meta_panel.clear()
        self.act_live.setChecked(False)
        self.current, self.companion, self._current_image = path, None, None
        self._full_image = self._full_wanted = None  # full resolution is for the open photo only
        if self.settings.value("last_dir") != str(path.parent):  # each write rewrites settings.ini
            self.settings.setValue("last_dir", str(path.parent))
        self.setWindowTitle(f"{path.name} - {APP_NAME}")

        self._awaiting = None
        self._cancel_queued_decodes()
        if kind is None:
            self._show_message(f"'{path.suffix or path.name}' is not a supported file type.\n"
                               "See Help > Supported formats.")
            self._schedule_slide()  # the slideshow skips files it cannot show
            return
        if not path.is_file():
            self.nav.remove(path)  # gone: also keeps the slideshow's next/wrap target valid
            self._show_message("The file cannot be found. It may have been moved or deleted.")
            self._schedule_slide()
            return
        self._add_recent(path)
        self.nav.set_current(path)
        self.filmstrip.set_files(self.nav.files, self.nav.index)
        self._show_overlay()
        if kind is MediaKind.VIDEO:
            self._show_video(path)
        elif (cached := self._cache.get(path)) is not None:
            self._on_decoded(self._token, cached)  # preloaded: show at once
            return
        else:
            self.status.setText(f"Loading {path.name}...")
            self._awaiting = (self._token, str(path))
            if str(path) not in self._inflight:  # a running preload of this file is reused, not repeated
                self._start_decode(path, PRIORITY_CURRENT)
        self._sync_actions()

    # -- background decoding and preloading (instant previous / next) ---------------
    def _start_decode(self, path: Path, priority: int) -> None:
        task = _DecodeTask(path, self._decode_signals, self._max_side)
        self._inflight[str(path)] = task
        self._pool.start(task, priority)

    def _request_full(self) -> None:
        """Zoom passed what the screen copy holds: decode the open photo at full resolution."""
        if self.current is None or not self.image_view.reduced or self._full_image is not None:
            return
        if self._full_wanted == (self._token, str(self.current)):
            return
        self._full_wanted = (self._token, str(self.current))
        self._pool.start(_DecodeTask(self.current, self._decode_signals, None), PRIORITY_CURRENT + 1)

    def _full_frame(self) -> QImage | None:
        """The frame on screen at full resolution, rotated as shown. Decodes now if needed (copy, save)."""
        if self.image_view.reduced and self.current is not None:
            if self._full_image is None:
                QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
                try:
                    self._full_image = decode(self.current)
                except DecodeError as exc:
                    log.warning("Full-resolution decode failed: %s", exc)
                    QMessageBox.warning(self, APP_NAME, str(exc))
                    return None
                finally:
                    QApplication.restoreOverrideCursor()
            self.image_view.upgrade(self._full_image)
        return self.image_view.current_frame()

    def _cancel_queued_decodes(self) -> None:
        """Drop decodes that have not started (stale files, old preloads). Running ones finish."""
        self._pool.clear()
        self._inflight = {p: t for p, t in self._inflight.items() if t.started}

    def _preload_neighbours(self) -> None:
        if not 0 <= self.nav.index < len(self.nav.files):
            return
        for offset in PRELOAD_OFFSETS:
            i = self.nav.index + offset
            if not 0 <= i < len(self.nav.files):
                continue
            path = self.nav.files[i]
            if (kind_of(path) in (None, MediaKind.VIDEO) or path.suffix.lower() in NO_PRELOAD_EXTS
                    or str(path) in self._inflight or path in self._cache):
                continue
            self._start_decode(path, PRIORITY_PRELOAD)

    def _on_task_finished(self, path: str, image: DecodedImage | None, key, message: str | None,
                          full: bool) -> None:
        if full:  # never cached: only the open photo keeps full resolution
            if self._full_wanted == (self._token, path):
                self._full_wanted = None
                if image is not None:
                    self._full_image = image
                    self.image_view.upgrade(image)
                else:
                    log.warning("Full-resolution decode failed: %s", message)
            return
        self._inflight.pop(path, None)
        if image is not None:
            self._cache.put(Path(path), image, key)
        if self._awaiting != (self._token, path):
            return  # a preload, or a file the user has already moved past
        self._awaiting = None
        if image is not None:
            self._on_decoded(self._token, image)
        else:
            log.warning("Decode failed: %s", message)
            self._show_message(message or f"{APP_NAME} cannot open this image.")
            self._schedule_slide()  # a damaged file must not stop the slideshow

    def _show_video(self, path: Path, force_loop: bool = False) -> None:
        self.stack.setCurrentIndex(self.PAGE_VIDEO)
        self.video_view.open(path, autoplay=True, force_loop=force_loop)
        self.status.setText("   |   ".join(p for p in (self.nav.position_text, path.name) if p))
        self._sync_actions()

    def _on_decoded(self, token: int, image: DecodedImage) -> None:
        if token != self._token:
            return  # a newer file was opened while this one decoded
        self.stack.setCurrentIndex(self.PAGE_IMAGE)
        self.image_view.set_image(image)
        self.meta_panel.set_items(image.metadata)
        self.companion = self.nav.companion_of(self.current) if self.current else None
        self._current_image = image
        self._sync_actions()
        self._update_status()
        self._preload_neighbours()
        self._schedule_slide()

    def _on_video_metadata(self, items: list) -> None:
        if self.act_live.isChecked():
            return  # keep the photo's metadata while previewing its motion clip
        self.meta_panel.set_items(file_items(self.current) + items if self.current else items)

    def _on_video_failed(self, message: str) -> None:
        log.warning("Video failed: %s", message)
        self._show_message(message)
        self._schedule_slide()

    def _show_welcome(self) -> None:
        self.welcome.set_recent(self._recent())
        self.stack.setCurrentIndex(self.PAGE_WELCOME)
        self.status.setText("")
        # Empty panels add nothing here; hide them until a file opens, without changing the user's choice.
        # In full screen they are already hidden, and restoreState brings the real choice back on exit.
        if self._docks_before_welcome is None and not self.immersive:
            # isHidden, not isVisible: before the window is shown nothing is "visible" yet.
            self._docks_before_welcome = (not self.meta_panel.isHidden(), not self.filmstrip.isHidden())
            self.meta_panel.hide()
            self.filmstrip.hide()
        self._sync_actions()

    def _leave_welcome(self) -> None:
        if self._docks_before_welcome is not None:
            meta, strip = self._docks_before_welcome
            self._docks_before_welcome = None
            self.meta_panel.setVisible(meta)
            self.filmstrip.setVisible(strip)

    def _show_message(self, text: str) -> None:
        self.message.setText(text)
        self.stack.setCurrentIndex(self.PAGE_MESSAGE)
        self.status.setText(self.nav.position_text)
        self._sync_actions()

    def _update_status(self) -> None:
        if self.stack.currentIndex() != self.PAGE_IMAGE or not self.current or not self._current_image:
            return
        img = self._current_image
        parts = [self.nav.position_text, self.current.name, img.format_name,
                 f"{img.size.width()} x {img.size.height()}", f"{self.image_view.zoom * 100:.0f}%"]
        if self.image_view.rotation:
            parts.append(f"rotated {self.image_view.rotation}°")
        if self.companion:
            parts.append("Live Photo (press L)")
        self.status.setText("   |   ".join(p for p in parts if p))

    # -- recent files ----------------------------------------------------------
    def _recent(self) -> list[str]:
        value = self.settings.value("recent", [])
        return [value] if isinstance(value, str) else list(value or [])

    def _add_recent(self, path: Path) -> None:
        key = str(path)
        items = [p for p in self._recent() if p.casefold() != key.casefold()]
        self.settings.setValue("recent", [key, *items][:MAX_RECENT])

    def _fill_recent_menu(self) -> None:
        self.recent_menu.clear()
        items = self._recent()
        for i, item in enumerate(items, 1):
            act = self.recent_menu.addAction(f"&{i % 10}  {Path(item).name}", lambda p=item: self.open_path(p))
            act.setToolTip(item)
            act.setStatusTip(item)
        if not items:
            self.recent_menu.addAction("No recent files").setEnabled(False)
        else:
            self.recent_menu.addSeparator()
            self.recent_menu.addAction("Clear list", lambda: self.settings.setValue("recent", []))

    # -- commands ------------------------------------------------------------
    def _rotate(self, degrees: int) -> None:
        self.image_view.rotate_by(degrees)
        self._update_status()

    def copy(self) -> None:
        tree = self.meta_panel.tree
        if tree.hasFocus() and tree.selectedItems():
            self.meta_panel.copy_selection()
            return
        clipboard = QGuiApplication.clipboard()
        frame = self._full_frame() if self.stack.currentIndex() == self.PAGE_IMAGE else None
        if frame is not None:
            clipboard.setImage(frame)
            self.statusBar().showMessage("Picture copied. Paste it into Teams, Outlook or any document.", 4000)
        elif self.current and self.current.is_file():
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(self.current))])
            clipboard.setMimeData(mime)
            self.statusBar().showMessage("File copied. Paste it into a folder, chat or email.", 4000)

    def copy_path(self) -> None:
        if self.current:
            QGuiApplication.clipboard().setText(str(self.current))
            self.statusBar().showMessage("File path copied.", 3000)

    def save_copy(self) -> None:
        image, src = self._current_image, self.current
        if image is None or src is None:
            return
        target, chosen = QFileDialog.getSaveFileName(
            self, "Save a copy", str(src.with_suffix(".jpg")), "JPEG image (*.jpg);;PNG image (*.png)")
        if not target:
            return
        frame = self._full_frame()  # after the file dialog: Cancel never costs a full decode
        if frame is None:
            return
        dest = Path(target)
        if dest.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            dest = dest.with_suffix(".png" if "PNG" in chosen else ".jpg")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            export_image(frame, dest, source=src, exif_bytes=image.exif_bytes,
                         strip_gps=self.act_strip_gps.isChecked())
        except ExportError as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "Save a copy", str(exc))
            return
        QApplication.restoreOverrideCursor()
        log.info("Saved copy as %s", describe_file(dest))
        self.statusBar().showMessage(f"Saved {dest.name}", 5000)
        if dest.parent == src.parent:  # show the new file in the strip
            self.nav.refresh()
            self.nav.set_current(src)
            self.filmstrip.set_files(self.nav.files, self.nav.index)
            self._sync_actions()
            self._update_status()

    def print_dialog(self) -> None:
        """Ctrl+P: pictures of this folder on A4 pages. Ctrl+click in the strip picks several."""
        files = [p for p in self.nav.files if _printable(p)]
        if not files:
            return
        chosen = {p for p in self.filmstrip.selected_files() if _printable(p)}
        if len(chosen) < 2:  # no multi-select: start with the open picture
            chosen = {self.current} if self.current in files else set()
        rotations = {}
        if self.current is not None and self.stack.currentIndex() == self.PAGE_IMAGE and self.image_view.rotation:
            rotations[self.current] = self.image_view.rotation
        if self.act_slideshow.isChecked():
            self.toggle_slideshow(False)
        if self.immersive:
            self.toggle_fullscreen(False)
        dialog = PrintDialog(files, chosen, rotations, self.settings, self)
        dialog.exec()
        dialog.deleteLater()  # a parented dialog would otherwise keep its previews until the app closes

    def delete_current(self) -> None:
        """Move the file (and a Live Photo's video) to the Recycle Bin. Never deletes permanently."""
        path, companion = self.current, self.companion
        if path is None or not path.exists():
            return
        self._token += 1
        self._full_image = self._full_wanted = None
        self.video_view.stop()  # release the file handle before moving it
        targets = [path] + ([companion] if companion and companion.exists() else [])
        failed = [p for p in targets if not QFile.moveToTrash(str(p))]
        if path in failed:
            QMessageBox.warning(self, "Move to Recycle Bin", (
                f"\"{path.name}\" could not be moved to the Recycle Bin, so it was not deleted.\n\n"
                "Network folders have no Recycle Bin, and the file may be open in another app."))
            self.open_path(path)
            return
        log.info("Moved %s to Recycle Bin", describe_file(path))
        self.filmstrip.invalidate(path)
        self._cache.discard(path)
        nxt = self.nav.remove_current()
        self.statusBar().showMessage(f"Moved {path.name} to the Recycle Bin.", 5000)
        if nxt is not None:
            self.open_path(nxt)
        else:
            self.current = self.companion = self._current_image = None
            self.image_view.clear()
            self.meta_panel.clear()
            self.filmstrip.set_files([], -1)
            self.setWindowTitle(APP_NAME)
            self._show_welcome()

    def set_theme(self, name: str) -> None:
        self._applying_theme = True
        self.theme = apply_theme(name)
        self._applying_theme = False
        self.settings.setValue("theme", self.theme)
        self._apply_icons()

    def toggle_live_photo(self, checked: bool) -> None:
        if not self.companion:
            return
        if checked:
            self._show_video(self.companion, force_loop=True)
        else:
            self.video_view.stop()
            self.stack.setCurrentIndex(self.PAGE_IMAGE)
            self._update_status()
        self._sync_actions()

    # -- distraction-free full screen ---------------------------------------------
    @property
    def immersive(self) -> bool:
        return self._saved_layout is not None

    def toggle_fullscreen(self, checked: bool) -> None:
        """Full screen shows only the photo or video. Menus, panels and the toolbar come back on exit."""
        if checked and not self.immersive:
            self._leave_welcome()  # snapshot the user's panels, not the welcome screen's empty ones
            self._saved_layout = self.saveState()
            self._saved_window_state = self.windowState()  # come back maximized if it was
            for bar in (self.menuBar(), self.statusBar(), self.findChild(QToolBar, "MainToolbar")):
                bar.hide()
            self.meta_panel.hide()
            self.filmstrip.hide()
            self.image_view.setBackgroundBrush(QColor("black"))  # photo viewers show photos on black
            self.showFullScreen()
            self._idle_timer.start()
            self._show_overlay()
        elif not checked and self.immersive:
            if self._saved_window_state & Qt.WindowState.WindowMaximized:
                self.showMaximized()
            else:
                self.showNormal()
            self.menuBar().show()
            self.statusBar().show()
            self.restoreState(self._saved_layout)  # toolbar and panels as they were
            self._saved_layout = None
            self.image_view.setBackgroundBrush(QBrush())
            self._idle_timer.stop()
            self._wake()
            self.overlay.hide()
        self.act_full.setChecked(self.immersive)

    def _on_idle(self) -> None:
        """Full screen, no mouse movement: hide the pointer and the video controls."""
        if self.immersive and not self._cursor_hidden:
            QApplication.setOverrideCursor(Qt.CursorShape.BlankCursor)
            self._cursor_hidden = True
            self.video_view.controls.hide()

    def _wake(self) -> None:
        if self._cursor_hidden:
            QApplication.restoreOverrideCursor()
            self._cursor_hidden = False
        self.video_view.controls.show()
        if self.immersive:
            self._idle_timer.start()

    def _show_overlay(self) -> None:
        if not self.immersive or self.current is None:
            return
        parts = [self.nav.position_text, self.current.name]
        if self.act_slideshow.isChecked():
            parts.append(f"Slideshow, {self.slideshow_seconds} s")
        self.overlay.setText("   ".join(p for p in parts if p))
        self.overlay.adjustSize()
        self.overlay.move(self.stack.mapToGlobal(QPoint(16, 16)))
        self.overlay.raise_()
        self.overlay.show()
        self._overlay_timer.start(OVERLAY_MS)

    def _on_escape(self) -> None:
        if self.act_slideshow.isChecked():
            self.toggle_slideshow(False)
        elif self.immersive:
            self.toggle_fullscreen(False)
        elif self.act_live.isChecked():
            self.act_live.setChecked(False)
            self.toggle_live_photo(False)

    # -- slideshow ----------------------------------------------------------------
    @property
    def slideshow_seconds(self) -> int:
        value = self._int_setting("slideshow_seconds", 5)
        return value if value in SLIDESHOW_SECONDS else 5

    def _int_setting(self, key: str, default: int) -> int:
        """A hand-edited or damaged settings.ini must never stop the app from starting."""
        try:
            return int(self.settings.value(key, default))
        except (TypeError, ValueError):
            log.warning("Ignored invalid setting %s", key)
            return default

    def toggle_slideshow(self, checked: bool) -> None:
        """F5: full screen, one file after another. Videos play to the end. Esc stops."""
        if checked:
            if not self.nav.files:
                self.act_slideshow.setChecked(False)
                self.statusBar().showMessage("Open a file or a folder first. Then start the slideshow.", 4000)
                return
            self.act_slideshow.setChecked(True)
            self.video_view.suppress_loop = True
            self._slideshow_entered_fullscreen = not self.immersive
            if not self.immersive:
                self.toggle_fullscreen(True)
            self._show_overlay()
            if self.stack.currentIndex() == self.PAGE_VIDEO:
                self.video_view._apply_loops()  # stop looping the clip that is already playing
            else:
                self._schedule_slide()
        else:
            self.act_slideshow.setChecked(False)
            self._slide_timer.stop()
            self.video_view.suppress_loop = False
            self.video_view._apply_loops()
            if self._slideshow_entered_fullscreen:
                self.toggle_fullscreen(False)
            self._slideshow_entered_fullscreen = False

    def _schedule_slide(self) -> None:
        """Images and error pages wait the interval; videos advance when they end."""
        if self.act_slideshow.isChecked() and self.stack.currentIndex() != self.PAGE_VIDEO:
            self._slide_timer.start(self.slideshow_seconds * 1000)

    def _on_video_ended(self) -> None:
        if self.act_slideshow.isChecked():
            self._slideshow_next()

    def _slideshow_next(self) -> None:
        if not self.act_slideshow.isChecked() or not self.nav.files:
            return
        nxt = self.nav.step(1) or self.nav.files[0]  # wrap to the start
        if nxt == self.current:  # a single file: show it again after the interval
            self._schedule_slide()
            return
        self.open_path(nxt)

    def reveal_in_explorer(self) -> None:
        if self.current and self.current.exists():
            subprocess.Popen(["explorer", "/select,", str(self.current)])  # noqa: S603,S607

    def show_shortcuts(self) -> None:
        QMessageBox.information(self, "Keyboard shortcuts", (
            "Ctrl+O   Open a file      Ctrl+Shift+O   Open a folder\n"
            "Left / Right, PgUp / PgDn   Previous / next file\n"
            "Home / End   First / last file in the folder\n"
            "Space   Next photo, or play/pause a video\n"
            "+ / -, mouse wheel   Zoom      F   Fit      1   Actual size\n"
            "R / Shift+R   Rotate right / left (the file is not changed)\n"
            "Ctrl+C   Copy picture      Ctrl+Shift+C   Copy file path\n"
            "Ctrl+S   Save a copy as JPG/PNG      Ctrl+P   Print (several pictures on one A4 page)\n"
            "Ctrl+click / Shift+click a thumbnail   Select several pictures to print\n"
            "Del   Move to Recycle Bin\n"
            "I   Metadata panel      T   Thumbnail strip\n"
            "F11   Full screen      F5   Slideshow      Esc   Leave full screen or slideshow\n"
            "L   Play Live Photo\n"
            "Shift+Left / Right   Back / forward 5 s      [ / ]   Slower / faster\n"
            "Ctrl+L   Loop video      M   Mute"))

    def show_formats(self) -> None:
        def join(exts):
            return ", ".join(e.lstrip(".").upper() for e in sorted(exts))

        QMessageBox.information(self, "Supported formats", (
            f"Images: {join(IMAGE_EXTS)}\n\nVector: {join(SVG_EXTS)}\n\n"
            f"Camera RAW: {join(RAW_EXTS)}\n\nVideo: {join(VIDEO_EXTS)}\n"
            "(H.265/HEVC, H.264, VP9, AV1, ProRes, MPEG-4 and more)"))

    def show_about(self) -> None:
        QMessageBox.about(self, f"About {APP_NAME}", (
            f"<b>{APP_NAME}</b> {__version__}<br><br>"
            "Offline viewer for HEIC, AVIF, JPEG XL, camera RAW and modern video.<br>"
            "No telemetry. Files never leave this computer.<br><br>"
            "Built with Qt (LGPL), Pillow, libheif, libjxl, LibRaw and FFmpeg.<br>"
            "See THIRD_PARTY_NOTICES.txt in the install folder.<br><br>"
            "<a href='https://github.com/xav-art/Media-reader-x'>github.com/xav-art/Media-reader-x</a>"))

    # -- events and settings ---------------------------------------------------
    def eventFilter(self, obj, event) -> bool:
        if self.immersive and event.type() in (QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress,
                                               QEvent.Type.Wheel):
            self._wake()
        if (event.type() == QEvent.Type.MouseButtonPress and self.isActiveWindow()
                and event.button() in (Qt.MouseButton.BackButton, Qt.MouseButton.ForwardButton)):
            self._go(self.nav.step(-1 if event.button() == Qt.MouseButton.BackButton else 1))
            return True
        return super().eventFilter(obj, event)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        urls = [u for u in event.mimeData().urls() if u.isLocalFile()]
        if urls:
            self.open_path(urls[0].toLocalFile())  # a folder opens its first file

    def _restore_settings(self) -> None:
        geometry = self.settings.value("geometry")
        if geometry is None or not self.restoreGeometry(geometry):
            self._fit_to_screen()
        state = self.settings.value("window_state")
        # Layouts saved before LAYOUT_VERSION had a much wider metadata panel: start fresh once.
        if state is not None and self._int_setting("layout_version", 1) >= LAYOUT_VERSION:
            self.restoreState(state)
        else:
            QTimer.singleShot(0, lambda: self.resizeDocks([self.meta_panel], [META_PANEL_PX],
                                                          Qt.Orientation.Horizontal))
        self.video_view.volume_percent = self._int_setting("volume", 80)
        self.video_view.loop_btn.setChecked(self.settings.value("video_loop", False, type=bool))
        self.act_strip_gps.setChecked(self.settings.value("strip_gps", False, type=bool))

    def _fit_to_screen(self) -> None:
        """First start: 1200 x 820, but never larger than 90% of the screen (small laptops, 150% scaling)."""
        screen = self.screen().availableGeometry()
        w = min(1200, int(screen.width() * 0.9))
        h = min(820, int(screen.height() * 0.9))
        self.resize(w, h)
        self.move(screen.x() + (screen.width() - w) // 2, screen.y() + (screen.height() - h) // 2)

    def closeEvent(self, event: QCloseEvent) -> None:
        QApplication.instance().removeEventFilter(self)
        if self.act_slideshow.isChecked():
            self.toggle_slideshow(False)
        if self.immersive:
            self.toggle_fullscreen(False)  # saves the normal layout, not the full-screen one
        self._leave_welcome()  # save the user's panel choice, not the welcome screen's
        self.video_view.stop()
        self.filmstrip.shutdown()
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("window_state", self.saveState())
        self.settings.setValue("layout_version", LAYOUT_VERSION)
        self.settings.setValue("volume", self.video_view.volume_percent)
        self._pool.clear()  # queued preloads and stale decodes: never start them
        self._pool.waitForDone(3000)  # only the decodes already running
        super().closeEvent(event)
