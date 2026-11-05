"""Media Reader x: a portable, offline viewer for modern image and video formats."""

import os

# numpy (pulled in by rawpy and imagecodecs) starts OpenBLAS with a buffer per CPU thread:
# measured +427 MB private memory on a 14-thread laptop, for linear algebra the app never uses.
# One thread costs 9 MB. Set before anything imports numpy.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
# Qt's FFmpeg build has network support. Allow only local files (Qt's default also allows crypto and data),
# so no file can make FFmpeg open a URL. Always set, never inherited: the environment cannot widen it.
os.environ["QT_FFMPEG_PROTOCOL_WHITELIST"] = "file"

__version__ = "1.6.0"
APP_NAME = "Media Reader x"
APP_ID = "MediaReaderX"
ORG_NAME = "xav-art"
