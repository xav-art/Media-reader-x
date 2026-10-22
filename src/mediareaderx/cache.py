"""Memory-capped cache of decoded images, so previous/next feel instant.

Keys include the file's size and modification time, so an edited file is
decoded again instead of showing stale pixels.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from mediareaderx.decoders import DecodedImage

DEFAULT_BUDGET_BYTES = 160 * 1024**2


def budget_for_screen(max_side: int, photos: int) -> int:
    """Room for ``photos`` screen copies of a 4:3 photo (4 bytes per pixel): about 19 MB each at
    2560 px, 44 MB at 3840 px (4K). Sized to the screen, so a 4K PC still preloads every neighbour
    and a smaller screen keeps less."""
    return photos * max_side * (max_side * 3 // 4) * 4


def cache_key(path: Path) -> tuple[str, int, int] | None:
    """Identity of a file version. Take it BEFORE decoding: a file replaced during the
    decode must not store old pixels under the new size and time."""
    try:
        st = path.stat()
    except OSError:
        return None
    return str(path).casefold(), st.st_size, st.st_mtime_ns


def _cost(image: DecodedImage) -> int:
    return sum(frame.sizeInBytes() for frame in image.frames)


class DecodedCache:
    def __init__(self, budget_bytes: int = DEFAULT_BUDGET_BYTES) -> None:
        self.budget = budget_bytes
        self._items: OrderedDict[tuple, tuple[DecodedImage, int]] = OrderedDict()
        self._used = 0

    def __contains__(self, path: Path) -> bool:
        key = cache_key(path)
        return key is not None and key in self._items

    def get(self, path: Path) -> DecodedImage | None:
        key = cache_key(path)
        if key is None or key not in self._items:
            return None
        self._items.move_to_end(key)
        return self._items[key][0]

    def put(self, path: Path, image: DecodedImage, key: tuple | None = None) -> None:
        """Store ``image``. Pass the ``key`` taken before decoding whenever you have it."""
        key, cost = key or cache_key(path), _cost(image)
        if key is None or cost > self.budget:
            return  # too big to keep: it is decoded again when needed
        if key in self._items:
            self._used -= self._items.pop(key)[1]
        self._items[key] = (image, cost)
        self._used += cost
        while self._used > self.budget:
            _old, (_img, old_cost) = self._items.popitem(last=False)
            self._used -= old_cost

    def discard(self, path: Path) -> None:
        target = str(path).casefold()
        for key in [k for k in self._items if k[0] == target]:
            self._used -= self._items.pop(key)[1]

    @property
    def used_bytes(self) -> int:
        return self._used
