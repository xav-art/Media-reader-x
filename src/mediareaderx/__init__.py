"""Media Reader x: a portable, offline viewer for modern image and video formats."""

import os

# numpy (pulled in by rawpy and imagecodecs) starts OpenBLAS with a buffer per CPU thread:
# measured +427 MB private memory on a 14-thread laptop, for linear algebra the app never uses.
# One thread costs 9 MB. Set before anything imports numpy.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

__version__ = "1.4.1"
APP_NAME = "Media Reader x"
APP_ID = "MediaReaderX"
ORG_NAME = "xav-art"
