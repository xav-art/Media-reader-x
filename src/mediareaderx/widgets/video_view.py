"""Video player built on Qt Multimedia's bundled FFmpeg backend.

The FFmpeg libraries ship inside PySide6, so HEVC, VP9 and AV1 play without
the Windows Store codec extensions and without admin rights.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaMetaData, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QSlider, QStyle, QToolButton, QVBoxLayout, QWidget

from mediareaderx import icons
from mediareaderx.metadata import MetaItem


SPEEDS = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
_ENUM_KEYS = (QMediaMetaData.Key.FileFormat, QMediaMetaData.Key.VideoCodec, QMediaMetaData.Key.AudioCodec)


def fmt_ms(ms: int) -> str:
    s = max(0, ms) // 1000
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class VideoView(QWidget):
    metadataReady = Signal(list)
    failed = Signal(str)
    ended = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.video = QVideoWidget(self)
        self.video.setAccessibleName("Video")
        self.player.setVideoOutput(self.video)
        self._force_loop = False

        style = self.style()
        self.play_btn = QToolButton(self)
        self.play_btn.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.play_btn.setToolTip("Play/Pause (Space)")
        self.play_btn.setAccessibleName("Play or pause")
        self.play_btn.clicked.connect(self.toggle_play)

        self.seek = QSlider(Qt.Orientation.Horizontal, self)
        self.seek.setAccessibleName("Playback position")
        self.seek.sliderMoved.connect(self.player.setPosition)
        self.time_label = QLabel("0:00 / 0:00", self)

        self.mute_btn = QToolButton(self)
        self.mute_btn.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_MediaVolume))
        self.mute_btn.setCheckable(True)
        self.mute_btn.setToolTip("Mute (M)")
        self.mute_btn.setAccessibleName("Mute")
        self.mute_btn.toggled.connect(self._on_mute)
        self.volume = QSlider(Qt.Orientation.Horizontal, self)
        self.volume.setRange(0, 100)
        self.volume.setFixedWidth(100)
        self.volume.setAccessibleName("Volume")
        self.volume.valueChanged.connect(lambda v: self.audio.setVolume(v / 100))
        self.volume.setValue(80)

        self.loop_btn = QToolButton(self)
        self.loop_btn.setText("Loop")
        self.loop_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.loop_btn.setCheckable(True)
        self.loop_btn.setToolTip("Repeat the video (Ctrl+L)")
        self.loop_btn.setAccessibleName("Loop")
        self.loop_btn.toggled.connect(lambda _on: self._apply_loops())

        self.speed_btn = QToolButton(self)
        self.speed_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        # The value itself ("1.5x") is the label; the menu arrow only crowds it.
        self.speed_btn.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0; }")
        self.speed_btn.setMinimumWidth(48)
        self.speed_btn.setToolTip("Playback speed ([ slower, ] faster)")
        self.speed_btn.setAccessibleName("Playback speed")
        menu = QMenu(self.speed_btn)
        for rate in SPEEDS:
            menu.addAction(f"{rate:g}x", lambda r=rate: self.set_rate(r))
        self.speed_btn.setMenu(menu)
        self.set_rate(1.0)

        self.controls = QWidget(self)  # one bar, so full screen can hide and show it as a unit
        controls = QHBoxLayout(self.controls)
        controls.setContentsMargins(8, 4, 8, 4)
        for w in (self.play_btn, self.seek, self.time_label, self.speed_btn, self.loop_btn,
                  self.mute_btn, self.volume):
            w.setParent(self.controls)
            controls.addWidget(w)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.video, 1)
        layout.addWidget(self.controls)
        self.suppress_loop = False  # the slideshow needs every clip to end

        self.player.durationChanged.connect(self._on_duration)
        self.player.positionChanged.connect(self._on_position)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.errorOccurred.connect(self._on_error)

    # -- public API --------------------------------------------------------------
    def open(self, path: str | Path, autoplay: bool = True, force_loop: bool = False) -> None:
        """Play ``path``. ``force_loop`` (Live Photos) loops regardless of the Loop button."""
        self._force_loop = force_loop
        self._apply_loops()
        self.set_rate(1.0)
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        if autoplay:
            self.player.play()

    def set_icon_color(self, color) -> None:
        """Re-tint the custom icons after a theme change."""
        self.loop_btn.setIcon(icons.icon("loop", color))

    def _apply_loops(self) -> None:
        looping = not self.suppress_loop and (self._force_loop or self.loop_btn.isChecked())
        self.player.setLoops(QMediaPlayer.Loops.Infinite if looping else QMediaPlayer.Loops.Once)

    @property
    def rate(self) -> float:
        return self.player.playbackRate()

    def set_rate(self, rate: float) -> None:
        self.player.setPlaybackRate(rate)
        self.speed_btn.setText(f"{rate:g}x")

    def change_speed(self, direction: int) -> None:
        """Step to the next slower (-1) or faster (+1) preset speed."""
        current = min(range(len(SPEEDS)), key=lambda i: abs(SPEEDS[i] - self.rate))
        self.set_rate(SPEEDS[max(0, min(len(SPEEDS) - 1, current + direction))])

    def stop(self) -> None:
        self.player.stop()
        self.player.setSource(QUrl())

    def toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def step(self, ms: int) -> None:
        self.player.setPosition(max(0, min(self.player.duration(), self.player.position() + ms)))

    def toggle_mute(self) -> None:
        self.mute_btn.toggle()

    @property
    def volume_percent(self) -> int:
        return self.volume.value()

    @volume_percent.setter
    def volume_percent(self, value: int) -> None:
        self.volume.setValue(max(0, min(100, int(value))))

    # -- slots -------------------------------------------------------------------
    def _on_mute(self, muted: bool) -> None:
        self.audio.setMuted(muted)
        icon = QStyle.StandardPixmap.SP_MediaVolumeMuted if muted else QStyle.StandardPixmap.SP_MediaVolume
        self.mute_btn.setIcon(self.style().standardIcon(icon))

    def _on_duration(self, duration: int) -> None:
        self.seek.setRange(0, duration)
        self._on_position(self.player.position())

    def _on_position(self, pos: int) -> None:
        if not self.seek.isSliderDown():
            self.seek.setValue(pos)
        self.time_label.setText(f"{fmt_ms(pos)} / {fmt_ms(self.player.duration())}")

    def _on_state(self, state) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        icon = QStyle.StandardPixmap.SP_MediaPause if playing else QStyle.StandardPixmap.SP_MediaPlay
        self.play_btn.setIcon(self.style().standardIcon(icon))

    def _on_status(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.LoadedMedia:
            self.metadataReady.emit(self.metadata_items())
        elif status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.ended.emit()
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self.failed.emit("This video could not be played. The file may be damaged or use an unsupported codec.")

    def _on_error(self, _error, message: str) -> None:
        self.failed.emit(message or "Playback failed.")

    def metadata_items(self) -> list[MetaItem]:
        md = self.player.metaData()
        rows = [MetaItem("Video", "Duration", fmt_ms(self.player.duration()))]

        def add(section: str, label: str, key) -> None:
            try:
                # PySide6 cannot convert enum-typed values (container, codecs)
                # through value(), so read those as Qt's display string.
                value = md.stringValue(key) if key in _ENUM_KEYS else md.value(key)
            except RuntimeError:
                return
            if value in (None, "", 0) or (isinstance(value, QSize) and value.isEmpty()):
                return
            if isinstance(value, QSize):
                value = f"{value.width()} x {value.height()} px"
            elif key in (QMediaMetaData.Key.VideoBitRate, QMediaMetaData.Key.AudioBitRate):
                value = f"{int(value) / 1000:.0f} kbit/s"
            elif key == QMediaMetaData.Key.VideoFrameRate:
                value = f"{float(value):.2f} fps"
            elif hasattr(value, "toString"):
                value = value.toString(Qt.DateFormat.ISODate)
            rows.append(MetaItem(section, label, str(value)))

        K = QMediaMetaData.Key
        for section, label, key in [
            ("Video", "Container", K.FileFormat), ("Video", "Resolution", K.Resolution),
            ("Video", "Video codec", K.VideoCodec), ("Video", "Frame rate", K.VideoFrameRate),
            ("Video", "Video bitrate", K.VideoBitRate), ("Video", "Orientation", K.Orientation),
            ("Video", "Audio codec", K.AudioCodec), ("Video", "Audio bitrate", K.AudioBitRate),
            ("Video", "Date", K.Date), ("Video", "Title", K.Title),
            ("Video", "Comment", K.Comment), ("Video", "Language", K.Language),
        ]:
            add(section, label, key)
        if not self.player.hasAudio():
            rows.append(MetaItem("Video", "Audio", "None"))
        return rows
