"""Detect AV1 video that cannot play on this PC.

Qt's bundled FFmpeg has only FFmpeg's own ``av1`` decoder, which works through the GPU (D3D11VA, D3D12VA or
DXVA2) and has no software path. Without that hardware, Qt reports no error: the clock runs and the clip ends,
but no frame arrives. The viewer and the thumbnailer use the rule here to turn that silence into a message.
"""

from __future__ import annotations

from PySide6.QtMultimedia import QMediaFormat, QMediaMetaData, QMediaPlayer

AV1_NAME = QMediaFormat.videoCodecName(QMediaFormat.VideoCodec.AV1)
NO_FRAME_AFTER_MS = 3000  # media time played with no frame before we call it a failure
MIN_CHECK_MS = 1000  # never decide earlier than this, except at the end of the clip
MAX_STEP_MS = 1000  # a larger position jump is a seek or a loop, not playing time
MESSAGE = ("Media Reader x cannot show this AV1 video. This PC may have no graphics hardware for AV1, "
           "or the file may be damaged.")

# Set when an AV1 clip played with no frame; the thumbnailer then skips AV1 at once. Any real AV1 frame clears it,
# so one damaged file on a PC that can decode AV1 does not stop the other AV1 thumbnails.
_unavailable = False


def is_av1(player: QMediaPlayer) -> bool:
    """True when the loaded media has a video track coded in AV1."""
    if not player.hasVideo():
        return False
    try:
        return player.metaData().stringValue(QMediaMetaData.Key.VideoCodec) == AV1_NAME
    except RuntimeError:
        return False


def played_step(previous_ms: int, position_ms: int) -> int:
    """Playing time between two position updates. Seeks and loop restarts count as zero."""
    step = position_ms - previous_ms
    return step if 0 < step <= MAX_STEP_MS else 0


def no_picture(played_ms: int, duration_ms: int, ended: bool) -> bool:
    """True when ``played_ms`` of playing (or the end of the clip) with no frame means the decoder never ran."""
    if ended:
        return True
    limit = NO_FRAME_AFTER_MS if duration_ms <= 0 else min(NO_FRAME_AFTER_MS, duration_ms * 4 // 5)
    return played_ms >= max(MIN_CHECK_MS, limit)


def mark_unavailable() -> None:
    global _unavailable
    _unavailable = True


def mark_available() -> None:
    global _unavailable
    _unavailable = False


def unavailable() -> bool:
    return _unavailable
