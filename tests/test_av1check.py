"""AV1 without GPU decoding: Qt reports nothing and shows a black video. The app must say why instead.

Unit tests drive the rule and VideoView's watch with injected positions and frames. Two subprocess tests play the
real AV1 sample: one with hardware decoding switched off (the failure must show), one with the default settings
(no false alarm when this PC can decode AV1; skipped on PCs and CI runners without AV1 hardware).
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from PySide6.QtMultimedia import QMediaPlayer

from mediareaderx import av1check
from mediareaderx.widgets.video_view import VideoView

SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture(autouse=True)
def fresh_session_flag(monkeypatch):
    monkeypatch.setattr(av1check, "_unavailable", False)


# -- the rule ---------------------------------------------------------------------
def test_codec_name_matches_what_qt_reports():
    assert av1check.AV1_NAME == "AV1"


@pytest.mark.parametrize("prev, pos, step", [(0, 40, 40), (1000, 2000, 1000), (0, 5000, 0), (3000, 100, 0), (5, 5, 0)])
def test_seeks_and_loop_restarts_are_not_playing_time(prev, pos, step):
    assert av1check.played_step(prev, pos) == step


@pytest.mark.parametrize("played, duration, ended, expected", [
    (2999, 60000, False, False), (3000, 60000, False, True),  # long clip: 3 s
    (2999, 0, False, False), (3000, 0, False, True),  # unknown duration: 3 s
    (1599, 2000, False, False), (1600, 2000, False, True),  # short clip: 80 %
    (999, 1000, False, False), (1000, 1000, False, True),  # never before 1 s ...
    (0, 500, True, True),  # ... except at the end of the clip
])
def test_no_picture(played, duration, ended, expected):
    assert av1check.no_picture(played, duration, ended) is expected


# -- VideoView watch (injected events) ----------------------------------------------
class _Frame:
    def __init__(self, valid):
        self._valid = valid

    def isValid(self):
        return self._valid


@pytest.fixture
def view(qapp, monkeypatch):
    v = VideoView()
    monkeypatch.setattr(v.player, "duration", lambda: 60000)
    v.failures, v.ends = [], []
    v.failed.connect(v.failures.append)
    v.ended.connect(lambda: v.ends.append(True))
    yield v
    v.deleteLater()


def test_no_frame_after_three_seconds_shows_the_message(view):
    view._arm_av1()
    for pos in range(0, 3001, 250):
        view._on_position(pos)
    assert view.failures == [av1check.MESSAGE]
    assert av1check.unavailable()
    assert not view._av1_watch and not view._frame_slot
    view._on_position(3250)
    assert len(view.failures) == 1  # once per file


def test_loaded_again_after_the_failure_does_not_arm_again(view, monkeypatch):
    monkeypatch.setattr(av1check, "is_av1", lambda player: True)
    view._on_status(QMediaPlayer.MediaStatus.LoadedMedia)
    assert view._av1_watch
    view._on_status(QMediaPlayer.MediaStatus.EndOfMedia)
    assert view.failures == [av1check.MESSAGE]
    view._on_status(QMediaPlayer.MediaStatus.LoadedMedia)  # Qt sends this from stop()
    assert not view._av1_watch and not view._frame_slot


def test_a_real_frame_ends_the_watch(view, monkeypatch):
    monkeypatch.setattr(av1check, "_unavailable", True)  # an earlier damaged file
    view._arm_av1()
    view._on_frame(_Frame(valid=False))  # Qt's empty frame on stop or a new source proves nothing
    assert view._av1_watch
    view._on_frame(_Frame(valid=True))
    assert not view._av1_watch and not view._frame_slot
    assert not av1check.unavailable()  # this PC decodes AV1: thumbnails may try again
    for pos in range(0, 10001, 250):
        view._on_position(pos)
    assert view.failures == []


def test_no_loop_while_watching_then_loop_again_after_a_frame(view):
    view.loop_btn.setChecked(True)
    assert view.player.loops() == QMediaPlayer.Loops.Infinite
    view._arm_av1()
    assert view.player.loops() == QMediaPlayer.Loops.Once  # so a stuck clip still reaches its end
    view.loop_btn.setChecked(False)
    view.loop_btn.setChecked(True)  # the user's toggle must not bring the loop back during the watch
    assert view.player.loops() == QMediaPlayer.Loops.Once
    view._on_frame(_Frame(valid=True))
    assert view.player.loops() == QMediaPlayer.Loops.Infinite


def test_seek_to_the_end_is_not_proof(view):
    view._arm_av1()
    view._on_position(0)
    view.step(60000)  # Shift+Right before the first frame
    view._on_position(55000)
    view._on_status(QMediaPlayer.MediaStatus.EndOfMedia)
    assert view.failures == [] and view.ends == [True]


def test_slider_seek_is_a_user_seek(view):
    view._arm_av1()
    view.seek.sliderMoved.emit(50000)
    view._on_status(QMediaPlayer.MediaStatus.EndOfMedia)
    assert view.failures == [] and view.ends == [True]


def test_a_jump_to_the_end_without_a_user_seek_is_proof(view):
    view._arm_av1()
    view._on_position(0)
    view._on_position(60000)  # a video-only clip with no decoder: the clock jumps to the end
    view._on_status(QMediaPlayer.MediaStatus.EndOfMedia)
    assert view.failures == [av1check.MESSAGE] and view.ends == []


def test_seek_is_not_playing_time(view):
    view._arm_av1()
    view._on_position(200)
    view._on_position(50000)  # the user drags the slider far ahead before any frame
    assert view.failures == []


def test_end_without_a_frame_fails_instead_of_ending(view):
    view._arm_av1()
    view._on_status(QMediaPlayer.MediaStatus.EndOfMedia)
    assert view.failures == [av1check.MESSAGE]
    assert view.ends == []  # the slideshow must not open the next file before the message


def test_end_after_a_frame_is_a_normal_end(view):
    view._arm_av1()
    view._on_frame(_Frame(valid=True))
    view._on_status(QMediaPlayer.MediaStatus.EndOfMedia)
    assert view.failures == [] and view.ends == [True]


def test_open_and_stop_disarm(view, tmp_path):
    view._arm_av1()
    view.stop()
    assert not view._av1_watch and not view._frame_slot
    view._arm_av1()
    view.open(tmp_path / "next.mp4", autoplay=False)
    assert not view._av1_watch and not view._frame_slot


def test_other_codecs_are_not_watched(view, samples):
    view.open(samples / "h264.mp4", autoplay=False)
    from PySide6.QtCore import QCoreApplication, QElapsedTimer
    t = QElapsedTimer()
    t.start()
    while view.player.mediaStatus() != QMediaPlayer.MediaStatus.LoadedMedia and t.elapsed() < 8000:
        QCoreApplication.processEvents()
    assert view.player.mediaStatus() == QMediaPlayer.MediaStatus.LoadedMedia
    assert not av1check.is_av1(view.player) and not view._av1_watch


def test_thumbnails_skip_av1_once_the_pc_proved_it_cannot(qapp, samples, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QElapsedTimer
    from mediareaderx.thumbnails import VideoThumbnailer

    monkeypatch.setattr(av1check, "_unavailable", True)
    real_is_av1, answers = av1check.is_av1, []
    monkeypatch.setattr(av1check, "is_av1", lambda player: answers.append(real_is_av1(player)) or answers[-1])
    thumbs = VideoThumbnailer()
    monkeypatch.setattr(thumbs, "TIMEOUT_MS", 60000)  # only the skip can finish this in time
    done = []
    thumbs.failed.connect(done.append)
    thumbs.ready.connect(lambda path, *_: done.append(("ready", path)))
    thumbs.request(str(samples / "av1.mkv"))
    t = QElapsedTimer()
    t.start()
    while not done and t.elapsed() < 15000:
        QCoreApplication.processEvents()
    assert done == [str(samples / "av1.mkv")]
    assert answers == [True]  # failed by the skip, not by an error or a damaged-file status


@pytest.mark.parametrize("position, ended, marked", [
    (1000 + 3000, False, True),  # played 3 s after the 1 s seek, no frame
    (1000 + 2000, False, False),  # 2 s is not enough proof (a busy PC)
    (1500, True, True),  # the clip ended with no frame
])
def test_thumbnail_timeout_marks_only_with_proof(qapp, monkeypatch, position, ended, marked):
    from mediareaderx.thumbnails import VideoThumbnailer

    thumbs = VideoThumbnailer()
    monkeypatch.setattr(av1check, "is_av1", lambda player: True)
    monkeypatch.setattr(thumbs._player, "position", lambda: position)
    monkeypatch.setattr(thumbs._player, "duration", lambda: 60000)
    status = QMediaPlayer.MediaStatus.EndOfMedia if ended else QMediaPlayer.MediaStatus.BufferedMedia
    monkeypatch.setattr(thumbs._player, "mediaStatus", lambda: status)
    failed = []
    thumbs.failed.connect(failed.append)
    thumbs._current, thumbs._start_pos = "clip.mkv", 1000
    thumbs._on_timeout()
    assert failed == ["clip.mkv"]
    assert av1check.unavailable() is marked


def test_live_photo_clip_failure_returns_to_the_photo(qapp, samples):
    from mediareaderx.main_window import MainWindow

    w = MainWindow()
    w.show()
    try:
        from PySide6.QtCore import QCoreApplication, QElapsedTimer
        w.open_path(samples / "IMG_0001.HEIC")
        t = QElapsedTimer()
        t.start()
        while w.stack.currentIndex() != w.PAGE_IMAGE and t.elapsed() < 15000:
            QCoreApplication.processEvents()
        w.act_live.trigger()
        assert w.stack.currentIndex() == w.PAGE_VIDEO
        w.video_view.failed.emit(av1check.MESSAGE)
        assert w.stack.currentIndex() == w.PAGE_IMAGE
        assert not w.act_live.isChecked()
        assert w.status.text() == av1check.MESSAGE
    finally:
        w.close()


# -- real playback in a subprocess (the env var must be set before Qt loads FFmpeg) ---------------------------
_PLAY = textwrap.dedent("""
    import json, sys
    from PySide6.QtCore import QCoreApplication, QElapsedTimer, QUrl
    from PySide6.QtMultimedia import QMediaPlayer, QVideoSink
    from PySide6.QtWidgets import QApplication
    from mediareaderx import av1check
    from mediareaderx.widgets.video_view import VideoView

    app = QApplication([])
    clip = sys.argv[1]

    # Reference: does this PC decode AV1 at all? A plain sink, no app code involved.
    ref = QMediaPlayer(); sink = QVideoSink(); ref.setVideoSink(sink); frames = []
    sink.videoFrameChanged.connect(lambda f: frames.append(f.isValid()))
    ref.setSource(QUrl.fromLocalFile(clip)); ref.play()

    view = VideoView(); failures = []
    view.failed.connect(failures.append)
    view.open(clip, force_loop=sys.argv[2] == "loop")
    t = QElapsedTimer(); t.start()
    while t.elapsed() < 12000 and not failures and view.player.mediaStatus() != QMediaPlayer.MediaStatus.EndOfMedia:
        QCoreApplication.processEvents()
    for _ in range(50):  # let stop()'s LoadedMedia arrive: it must not arm the watch again
        QCoreApplication.processEvents()
    print(json.dumps({"reference_frames": sum(frames), "failures": failures, "watching": view._av1_watch,
                      "stopped": view.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState}))
""")


def _play(tmp_path, clip, no_hardware, loop=False):
    script = tmp_path / "play_av1.py"
    script.write_text(_PLAY, encoding="utf-8")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=str(SRC))
    if no_hardware:
        env["QT_FFMPEG_DECODING_HW_DEVICE_TYPES"] = ","  # an empty list: no hardware decoder at all
    else:
        env.pop("QT_FFMPEG_DECODING_HW_DEVICE_TYPES", None)
    out = subprocess.run([sys.executable, str(script), str(clip), "loop" if loop else "once"], env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_av1_without_hardware_shows_the_message(tmp_path, samples):
    result = _play(tmp_path, samples / "av1.mkv", no_hardware=True)
    assert result["reference_frames"] == 0  # proves the env var really removed the decoder
    assert result["failures"] == [av1check.MESSAGE]
    assert result["stopped"] and not result["watching"]


def test_looping_video_only_av1_without_hardware_shows_the_message(tmp_path):
    # No audio: the position does not move without a video decoder, and a looping clip never ends.
    import make_samples
    clip = tmp_path / "av1-video-only.mkv"
    make_samples.make_video(clip, "libsvtav1", audio=None)
    result = _play(tmp_path, clip, no_hardware=True, loop=True)
    assert result["reference_frames"] == 0
    assert result["failures"] == [av1check.MESSAGE]
    assert result["stopped"] and not result["watching"]


def test_av1_with_hardware_plays_without_a_message(tmp_path, samples):
    result = _play(tmp_path, samples / "av1.mkv", no_hardware=False)
    if result["reference_frames"] == 0:
        pytest.skip("this PC has no AV1 hardware decoding")
    assert result["failures"] == []
