"""Protected decoding: decoders run in locked-down helper processes (see docs/SANDBOX.md).

The app calls ``decode``, ``make_thumbnail``, ``check_video`` and ``video_thumbnail`` here instead of the
decoders. Each call opens the file in this process, hands a read-only handle to a helper, and checks
every byte of the reply before it becomes a picture.

Policy when no helper can start (``state``):
- "on": helpers work.
- "fallback": helpers never worked on this PC (for example a policy blocks AppContainers): decode in this
  process, as Media Reader x 1.5 did, and say so once.
- "failed": helpers worked on this PC before, and now cannot start. Files are not opened ("fail closed"),
  so nothing can force the app to drop the protection. A new attempt is made after ``RETRY_S``.
- "off": turned off for development (environment variable MEDIAREADERX_SANDBOX=off).
"""

from __future__ import annotations

import json
import logging
import msvcrt
import os
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Signal

from mediareaderx import config
from mediareaderx.decoders import DecodedImage, DecodeError
from mediareaderx.metadata import MetaItem, file_items
from mediareaderx.sandbox import protocol

log = logging.getLogger(__name__)

CAPABILITY = "xavArtMediaReaderXFiles"  # helpers hold it; the app folder lets it read and execute
SLOT_PREFIX = "xav-art.MediaReaderX.Helper"
OLD_PROFILES = ("xav-art.MediaReaderX.Decoder",)  # the 1.6 prototype's profile name
SLOTS = 16  # shared by every running copy of the app: a slot is claimed with a per-user lock
READY_TIMEOUT_S = 45
IDLE_EXIT_S = 120
ACQUIRE_WAIT_S = 30  # longest wait for a free helper; then "busy" (the GUI thread never waits for minutes)
RETRY_S = 30
GB = 1 << 30

# role: (most helpers at once, memory limit per helper, reused for more files)
ROLES = {"view": (2, 4 * GB, True), "thumb": (2, 1 * GB, True), "check": (3, 1 * GB, False)}
SPARES = {"view": 1, "thumb": 1, "check": 2}  # started ahead and kept ready, so a file does not wait
TIMEOUTS = {"decode": 60, "decode_full": 120, "thumb": 20, "video": 25}

BUSY = "Media Reader x is still busy with other files. Try again in a moment."
UNAVAILABLE = ("Media Reader x could not start its protected decoder, so it did not open this file. "
               "To see why, select Help > Open log folder.")
FALLBACK_NOTICE = "Protected decoding is not available on this PC. Files open without it."
CRASHED = "The file is damaged or cannot be read. The decoder stopped while reading it."
TOO_BIG = "This file needs more memory than Media Reader x allows on this PC."


class Notifier(QObject):
    stateChanged = Signal(str)  # emitted from any thread; connect on the GUI thread (queued)


notifier = Notifier()


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_roots() -> list[Path]:
    """Folders a helper must be able to read: the installed app, or (from source) Python and the code."""
    if _frozen():
        return [Path(sys.executable).resolve().parent]
    import mediareaderx

    roots = {Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve(),
             Path(mediareaderx.__file__).resolve().parent.parent}
    return sorted(roots)


class _Helper:
    def __init__(self, slot: int, role: str):
        self.slot, self.role = slot, role
        self.proc = self.job = None
        self.to_helper = self.from_helper = None
        self.limit = ROLES[role][1]
        self.claim = None  # the per-user lock on the slot, held while the helper lives
        self.busy = False
        self.served = False  # has received a file: a one-shot helper is then never reused
        self.uses = 0
        self.last_used = time.monotonic()

    def kill(self) -> None:
        from mediareaderx.sandbox import winapi

        if self.proc:
            winapi.terminate(self.proc)

    def close(self) -> None:
        from mediareaderx.sandbox import winapi

        self.kill()
        for stream in (self.to_helper, self.from_helper):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        self.to_helper = self.from_helper = None
        if self.proc:
            winapi.wait(self.proc, 2000)
        winapi.close(self.proc)
        winapi.close(self.job)
        winapi.close(self.claim)
        self.proc = self.job = self.claim = None


class NoSlot(OSError):
    """Every helper slot is in use (by this app or another running copy): wait, it is not a failure."""


class Unavailable(Exception):
    """No helper could start, and the policy says not to decode in this process."""


class Busy(Exception):
    """Every helper for this kind of work stayed busy for ``ACQUIRE_WAIT_S``."""


class UseInProcess(Exception):
    """Protected decoding is off or never worked on this PC: the caller uses its in-process code."""


class _Pool:
    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.helpers: list[_Helper] = []
        self.starting = {role: 0 for role in ROLES}
        self.free_slots = list(range(SLOTS))
        self.state = "off" if os.environ.get("MEDIAREADERX_SANDBOX", "").lower() == "off" else "new"
        self.worked_before = False
        self.failed_at = 0.0
        self.desktop_handle = None
        self.desktop_name = ""
        self.outer_job = None
        self.cap = None
        self.setup_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.generation = 0  # bumped by shutdown: a start that a shutdown interrupts never counts as a failure
        self.reaper: threading.Thread | None = None

    # -- one-time setup -----------------------------------------------------------------------------
    def _setup(self) -> None:
        from mediareaderx.sandbox import winapi

        with self.setup_lock:
            if self.cap is None:
                self._setup_locked(winapi)

    def _setup_locked(self, winapi) -> None:
        cap = winapi.capability(CAPABILITY)
        for root in app_roots():
            probe = Path(sys.executable).resolve() if _frozen() else root
            if not (winapi.has_read_access(root, cap) and winapi.has_read_access(probe, cap)):
                log.info("Granting the protected decoder read access to the app folder")
                winapi.grant_read(root, cap)
        total = max(2 * GB, min(8 * GB, winapi.physical_memory() // 2))
        self.outer_job = winapi.helpers_job(total)
        self.desktop_name = f"MediaReaderX-{os.getpid()}"
        self.desktop_handle = winapi.private_desktop(self.desktop_name, cap)
        self.cap = cap
        if self.reaper is None:
            self.reaper = threading.Thread(target=self._reap, name="sandbox-reaper", daemon=True)
            self.reaper.start()

    def _command(self, read_end: int, write_end: int, role: str) -> tuple[str, list[str], dict[str, str], str]:
        env = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
               "PATH": os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32"),
               "OPENBLAS_NUM_THREADS": "1", "QT_QPA_PLATFORM": "offscreen", "QT_FFMPEG_PROTOCOL_WHITELIST": "file",
               "QT_FFMPEG_DECODING_HW_DEVICE_TYPES": ",",  # no graphics driver inside the helper
               "PYTHONDONTWRITEBYTECODE": "1", "MRX_HELPER_ROLE": role}
        handles = [str(read_end), str(write_end)]
        if _frozen():
            exe = str(Path(sys.executable).resolve())
            return exe, ["--sandbox-helper", *handles], env, str(Path(exe).parent)
        import mediareaderx

        env["MRX_SYS_PATH"] = json.dumps([p for p in sys.path if p and os.path.isdir(p)])
        boot = ("import json,os,sys;sys.path[0:0]=json.loads(os.environ['MRX_SYS_PATH']);"
                "from mediareaderx.sandbox.worker import main;sys.exit(main(sys.argv[1:]))")
        src = str(Path(mediareaderx.__file__).resolve().parent.parent)
        return str(Path(sys._base_executable).resolve()), ["-I", "-c", boot, *handles], env, src

    def _spawn(self, role: str) -> _Helper:
        """Start a helper and wait for its "ready". Raises OSError or protocol errors."""
        from mediareaderx.sandbox import winapi

        # Claim a slot no other running copy of the app holds: two live helpers never share a container.
        with self.cond:
            candidates = list(self.free_slots)
        slot = claim = None
        for candidate in candidates:
            claim = winapi.claim_slot(f"Global\\{SLOT_PREFIX}.{winapi.user_sid_string()}.{candidate}")
            if claim is None:
                continue
            with self.cond:
                if candidate in self.free_slots:
                    self.free_slots.remove(candidate)
                    slot = candidate
                    break
            winapi.close(claim)
        if slot is None:
            raise NoSlot("no free helper slot")
        helper = _Helper(slot, role)
        helper.claim = claim
        child_ends: list[int] = []
        try:
            self._setup()
            container = winapi.fresh_container(f"{SLOT_PREFIX}.{slot}")
            # The real %LOCALAPPDATA% (Windows derives the helper's TEMP from it), even when this process runs
            # with another one (tests isolate it): ...\Packages\<name>\AC -> three levels up.
            local_appdata = str(Path(winapi.container_folder(container)).parents[2])
            to_r, to_w = winapi.pipe(child_reads=True)
            child_ends = [to_r, to_w]  # closed on failure until the streams own the parent's ends
            from_r, from_w = winapi.pipe(child_reads=False)
            child_ends = [to_r, from_w, to_w, from_r]
            helper.to_helper = os.fdopen(msvcrt.open_osfhandle(to_w, os.O_WRONLY | os.O_BINARY), "wb", buffering=0)
            helper.from_helper = os.fdopen(msvcrt.open_osfhandle(from_r, os.O_RDONLY | os.O_BINARY), "rb",
                                           buffering=0)
            child_ends = [to_r, from_w]  # the parent's ends now belong to the streams
            helper.job = winapi.helper_job(helper.limit)
            exe, args, env, cwd = self._command(to_r, from_w, role)
            env["LOCALAPPDATA"] = local_appdata
            helper.proc, _pid = winapi.launch(container, self.cap, exe, args, env, child_ends, cwd,
                                              self.desktop_name, [self.outer_job, helper.job])
            for end in child_ends:
                winapi.close(end)
            child_ends = []
            timer = threading.Timer(READY_TIMEOUT_S, helper.kill)
            timer.start()
            try:
                hello = protocol.read_header(helper.from_helper)
            finally:
                timer.cancel()
            if hello.get("op") != "ready" or hello.get("blobs"):
                raise protocol.ProtocolError("no ready message")
            return helper
        except BaseException:
            for end in child_ends:
                winapi.close(end)
            code = winapi.exit_code(helper.proc) if helper.proc else None
            helper.close()
            with self.cond:
                self.free_slots.append(slot)
            if code is not None:
                log.warning("Protected decoder exited during start (code 0x%08x)", code & 0xFFFFFFFF)
            raise

    # -- policy -----------------------------------------------------------------------------------------
    def _set_state(self, state: str) -> None:
        with self.state_lock:  # a failing start and a working one can race: once one worked, never "fallback"
            if state == "on":
                self.worked_before = True  # from now on, this session fails closed too
            elif state == "fallback" and self.worked_before:
                state = "failed"
            if state == self.state:
                return
            self.state = state
        log.info("Protected decoding: %s", state)
        try:
            notifier.stateChanged.emit(state)
        except RuntimeError:
            pass

    def _start_failed(self, exc: BaseException) -> None:
        """Only a PC where no helper has ever started falls back to in-process decoding. Once one worked (on this
        PC before, or in this session), a failure fails closed, whatever caused it."""
        log.warning("Protected decoder could not start: %s", exc)
        with self.cond:
            self.failed_at = time.monotonic()
        self._set_state("failed" if self.worked_before or self.state == "on" else "fallback")

    def usable(self) -> bool:
        """True when requests should go to helpers (or try to)."""
        if self.state in ("off", "fallback"):
            return False
        if self.state == "failed" and time.monotonic() - self.failed_at < RETRY_S:
            raise Unavailable()
        return True

    # -- helpers ------------------------------------------------------------------------------------------
    def _drop_dead(self) -> None:
        """Forget idle helpers that exited (an antivirus or the user may end them): never hand them a file."""
        from mediareaderx.sandbox import winapi

        with self.cond:
            dead = [h for h in self.helpers if not h.busy and h.proc and winapi.exit_code(h.proc) is not None]
            for h in dead:
                self.helpers.remove(h)
        for h in dead:
            log.warning("A protected decoder ended on its own; starting a new one")
            h.close()
            with self.cond:
                self.free_slots.append(h.slot)
                self.cond.notify_all()

    def acquire(self, role: str) -> _Helper:
        most = ROLES[role][0]
        self._drop_dead()
        deadline = time.monotonic() + ACQUIRE_WAIT_S
        while True:
            if time.monotonic() > deadline:
                raise Busy()
            with self.cond:
                for helper in self.helpers:
                    if helper.role == role and not helper.busy:
                        helper.busy = True
                        return helper
                live = sum(1 for h in self.helpers if h.role == role) + self.starting[role]
                if live < most and self.free_slots:
                    self.starting[role] += 1
                    generation = self.generation
                else:
                    self.cond.wait(1.0)
                    continue
            try:
                helper = self._spawn(role)
            except BaseException as exc:
                with self.cond:
                    self.starting[role] -= 1
                    self.cond.notify_all()
                    stale = generation != self.generation
                if stale:
                    continue  # a shutdown ran meanwhile: try again with the new setup
                if isinstance(exc, NoSlot):
                    with self.cond:
                        self.cond.wait(0.5)
                    continue
                self._start_failed(exc)
                raise Unavailable() from exc
            if not self._adopt(helper, role, generation, busy=True):
                continue
            if self.state != "on":
                self._set_state("on")
            return helper

    def _adopt(self, helper: _Helper, role: str, generation: int, busy: bool) -> bool:
        """Add a started helper to the pool, unless a shutdown ran while it started (then end it)."""
        with self.cond:
            self.starting[role] -= 1
            current = generation == self.generation
            if current:
                helper.busy = busy
                self.helpers.append(helper)
            self.cond.notify_all()
        if not current:
            helper.close()
            with self.cond:
                self.free_slots.append(helper.slot)
                self.cond.notify_all()
        return current

    def release(self, helper: _Helper, keep: bool) -> None:
        with self.cond:
            helper.busy = False
            helper.uses += 1
            helper.last_used = time.monotonic()
            owned = helper in self.helpers  # False when a shutdown already ended it and freed its slot
            reusable = owned and keep and (ROLES[helper.role][2] or not helper.served)
            if owned and not reusable:
                self.helpers.remove(helper)
            self.cond.notify_all()
        if owned and not reusable:
            helper.close()
            with self.cond:
                self.free_slots.append(helper.slot)
                self.cond.notify_all()
            if helper.role == "check":  # keep one fresh checker ready, so opening a video does not wait
                self.prewarm("check")

    def prewarm(self, role: str) -> None:
        """Start helpers in the background until ``SPARES[role]`` unused ones are ready."""
        def run() -> None:
            try:
                if not self.usable():
                    return
            except Unavailable:
                return
            while True:
                with self.cond:
                    mine = [h for h in self.helpers if h.role == role]
                    spares = sum(1 for h in mine if not h.busy and not h.served) + self.starting[role]
                    if (spares >= SPARES[role] or len(mine) + self.starting[role] >= ROLES[role][0]
                            or not self.free_slots):
                        return
                    self.starting[role] += 1
                    generation = self.generation
                try:
                    helper = self._spawn(role)
                except BaseException as exc:
                    with self.cond:
                        self.starting[role] -= 1
                        self.cond.notify_all()
                        stale = generation != self.generation
                    if not stale and not isinstance(exc, NoSlot):
                        self._start_failed(exc)
                    return
                if not self._adopt(helper, role, generation, busy=False):
                    return
                if self.state != "on":
                    self._set_state("on")

        threading.Thread(target=run, name=f"sandbox-prewarm-{role}", daemon=True).start()

    def _reap(self) -> None:
        while True:
            time.sleep(15)
            now = time.monotonic()
            idle = []
            with self.cond:
                for role in ROLES:
                    mine = [h for h in self.helpers if h.role == role and not h.busy]
                    for h in mine[SPARES[role]:]:
                        if now - h.last_used > IDLE_EXIT_S:
                            idle.append(h)
                for h in idle:
                    self.helpers.remove(h)
            for h in idle:
                h.close()
                with self.cond:
                    self.free_slots.append(h.slot)
                    self.cond.notify_all()

    def shutdown(self) -> None:
        from mediareaderx.sandbox import winapi

        with self.setup_lock:
            with self.cond:
                self.generation += 1
                helpers, self.helpers = self.helpers, []
            for h in helpers:
                h.close()
                with self.cond:
                    self.free_slots.append(h.slot)
            # Start from scratch next time: a new desktop and job (helpers must never point at a closed desktop).
            if self.desktop_handle:
                winapi.close_desktop(self.desktop_handle)
            winapi.close(self.outer_job)
            self.desktop_handle = self.outer_job = self.cap = None
            with self.cond:
                self.cond.notify_all()


_pool = _Pool()


def configure(worked_before: bool) -> None:
    """``worked_before``: helpers have started on this PC before (stored by the window in settings.ini)."""
    _pool.worked_before = bool(worked_before)


def worked_before() -> bool:
    return _pool.worked_before


def state() -> str:
    return _pool.state


def start() -> None:
    """Start a viewer helper and a video checker in the background, so the first file does not wait."""
    if sys.platform == "win32" and _pool.state != "off":
        _pool.prewarm("view")
        _pool.prewarm("check")


def shutdown() -> None:
    _pool.shutdown()


def remove_profiles() -> None:
    """Delete the helpers' AppContainer profiles (``--remove-sandbox``, run by the uninstaller)."""
    from mediareaderx.sandbox import winapi

    for name in [f"{SLOT_PREFIX}.{i}" for i in range(SLOTS)] + list(OLD_PROFILES):
        winapi.delete_container(name)


# -- requests -----------------------------------------------------------------------------------------
def _request(role: str, op: str, path: Path, extra: dict, timeout: int, read_reply):
    """Send ``op`` for ``path`` to a helper and return ``read_reply(header, stream)``. Raises DecodeError."""
    from mediareaderx import content
    from mediareaderx.sandbox import winapi

    try:
        handle, _size, _head = winapi.open_for_read(path, prefetch=4096)
    except OSError as exc:
        raise DecodeError("The file cannot be read. It may have been moved or you may lack access.") from exc
    try:
        helper = _pool.acquire(role)
    except (Unavailable, Busy):
        winapi.close(handle)
        raise
    keep = False
    expired = threading.Event()

    def expire() -> None:
        expired.set()  # before the kill: whoever sees the pipe close also sees why
        helper.kill()

    timer = threading.Timer(timeout, expire)
    try:
        helper.served = True
        handle, giving = None, handle  # DuplicateHandle closes the source even when it fails: never close it twice
        remote = winapi.give_handle(helper.proc, giving)
        protocol.write_message(helper.to_helper, {"op": op, "h": remote, "ext": path.suffix.lower()[:16], **extra})
        timer.start()
        header = protocol.read_header(helper.from_helper)
        notes = header.get("notes")
        for note in notes[:20] if isinstance(notes, list) else []:
            if isinstance(note, str):
                log.info("Decoder: %s", protocol.clean_text(note, 200))
        if header.get("ok") is not True:  # a clean refusal: the helper is fine and stays
            if _blobs(header):
                raise protocol.ProtocolError("blobs with an error")
            message = protocol.clean_text(header.get("error") or "The file cannot be opened.")
            keep = not expired.is_set()
            raise DecodeError(TOO_BIG if header.get("memory") is True else message)
        try:
            result = read_reply(header, helper.from_helper)
        except (protocol.ProtocolError, protocol.HelperGone, DecodeError, MemoryError, content.Refused):
            raise  # Refused: the helper's reason for a video, shown to the user as it is
        except Exception as exc:  # anything a malformed reply can raise: treat it as a broken rule
            raise protocol.ProtocolError(type(exc).__name__) from None
        keep = not expired.is_set()  # a kill that landed after the reply: do not reuse a dying helper
        return result
    except (protocol.HelperGone, OSError) as exc:
        if helper.proc:
            winapi.wait(helper.proc, 2000)  # the pipe closes before the exit code is set
        timed_out = expired.is_set()
        peak = winapi.job_peak_memory(helper.job) if helper.job else 0
        code = winapi.exit_code(helper.proc) if helper.proc else None
        log.warning("Protected decoder stopped during %s (%s, code %s)", op, type(exc).__name__,
                    "none" if code is None else f"0x{code & 0xFFFFFFFF:08x}")
        if timed_out:
            raise DecodeError("The file took too long to read. It may be damaged.") from None
        if peak >= helper.limit * 0.9:
            raise DecodeError(TOO_BIG) from None
        raise DecodeError(CRASHED) from None
    except protocol.ProtocolError as exc:
        log.warning("Protected decoder sent a bad reply (%s)", exc)
        raise DecodeError("The file is damaged or cannot be read.") from None
    finally:
        timer.cancel()
        if handle:
            winapi.close(handle)
        _pool.release(helper, keep)


def _blobs(header: dict) -> list[int]:
    return list(header.get("blobs", []))


def _read_frames(header: dict, f, max_frames: int, max_pixels: int, max_total: int) -> list:
    frames = header.get("frames")
    blobs = _blobs(header)
    if not isinstance(frames, list) or len(frames) > max_frames or len(blobs) < len(frames):
        raise protocol.ProtocolError("bad frames")
    if sum(blobs[:len(frames)]) > max_total:
        raise protocol.ProtocolError("too many pixels")
    return [protocol.read_image(f, spec, size, max_pixels) for spec, size in zip(frames, blobs)]


def _read_decoded(header: dict, f) -> DecodedImage:
    frames_spec = header.get("frames")
    count = len(frames_spec) if isinstance(frames_spec, list) else 0
    exif_len = header.get("exif", 0)
    blobs = _blobs(header)
    if not 1 <= count or not protocol._is_int(exif_len, 0, protocol.MAX_EXIF):
        raise protocol.ProtocolError("bad decode reply")
    if len(blobs) != count + (1 if exif_len else 0) or (exif_len and blobs[-1] != exif_len):
        raise protocol.ProtocolError("bad blob list")
    durations = header.get("durations")
    if not isinstance(durations, list) or len(durations) != count or not all(
            protocol._is_int(d, 0, 600_000) for d in durations):
        raise protocol.ProtocolError("bad durations")
    rows = header.get("metadata")
    if not isinstance(rows, list) or len(rows) > protocol.MAX_META_ROWS:
        raise protocol.ProtocolError("bad metadata")
    metadata = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 3:
            raise protocol.ProtocolError("bad metadata row")
        metadata.append(MetaItem(protocol.clean_text(row[0], 60), protocol.clean_text(row[1], 100),
                                 protocol.clean_text(row[2], protocol.MAX_META_TEXT)))
    full = header.get("full_size")
    if full is not None and (not isinstance(full, list) or len(full) != 2
                             or not all(protocol._is_int(v, 1, 1_000_000) for v in full)):
        raise protocol.ProtocolError("bad size")
    format_name = protocol.clean_text(header.get("format_name", ""), 60)
    notice = protocol.optional_text(header, "notice")
    total = config.MAX_ANIMATION_BYTES if count > 1 else config.MAX_IMAGE_PIXELS * 4
    frames = _read_frames(header, f, config.MAX_ANIMATION_FRAMES, config.MAX_IMAGE_PIXELS, total)
    exif = whitelist_exif(protocol.read_exact(f, exif_len)) if exif_len else None
    return DecodedImage(frames, durations, format_name, metadata, exif, QSize(*full) if full else None, notice)


# EXIF a saved copy may carry, rebuilt here from typed values: nothing else from a helper reaches a file.
_EXIF_KEEP = {0x010F, 0x0110, 0x0112, 0x011A, 0x011B, 0x0128, 0x0132, 0x013B, 0x8298}
_EXIF_SUB_KEEP = {0x9000, 0x829A, 0x829D, 0x8822, 0x8827, 0x9003, 0x9004, 0x9010, 0x9011, 0x9012, 0x9201, 0x9202,
                  0x9204, 0x9205, 0x9207, 0x9209, 0x920A, 0x9290, 0x9291, 0x9292, 0xA001, 0xA002, 0xA003,
                  0xA402, 0xA403, 0xA405, 0xA406, 0xA433, 0xA434}
_EXIF_IFD, _GPS_IFD = 0x8769, 0x8825
_EXIF_TEXT_MAX = 64


def _keep_value(value):
    """A plain EXIF value (short text, number, rational, a few of them, or a few bytes), else None."""
    from PIL.TiffImagePlugin import IFDRational

    if isinstance(value, str):
        return protocol.clean_text(value, _EXIF_TEXT_MAX)
    if isinstance(value, bytes):
        return value if len(value) <= 8 else None  # GPS version, altitude reference, EXIF version
    if isinstance(value, (int, float, IFDRational)):
        return value
    if isinstance(value, tuple) and len(value) <= 8 and all(isinstance(v, (int, float, IFDRational)) for v in value):
        return value
    return None


def whitelist_exif(raw: bytes) -> bytes | None:
    """EXIF for "Save a copy": camera, exposure, date and GPS tags with plain values, rebuilt from scratch."""
    from PIL import Image

    try:
        src = Image.Exif()
        src.load(raw)
        out = Image.Exif()
        for tag in _EXIF_KEEP:
            if tag in src and (value := _keep_value(src[tag])) is not None:
                out[tag] = value
        sub = {tag: kept for tag, value in src.get_ifd(_EXIF_IFD).items()
               if tag in _EXIF_SUB_KEEP and (kept := _keep_value(value)) is not None}
        if sub:
            out[_EXIF_IFD] = sub
        gps = {tag: kept for tag, value in src.get_ifd(_GPS_IFD).items()
               if 0 <= tag <= 31 and (kept := _keep_value(value)) is not None}
        if gps:
            out[_GPS_IFD] = gps
        return out.tobytes() if len(out) else None
    except Exception:
        log.debug("EXIF dropped: not readable", exc_info=True)
        return None


def _read_thumb(header: dict, f):
    frames = _read_frames(header, f, 1, 4096 * 4096, 4096 * 4096 * 4)
    return frames[0] if frames else None


# -- public API -------------------------------------------------------------------------------------------
def decode(path: str | Path, max_side: int | None = None) -> DecodedImage:
    """``decoders.decode``, protected. Raises DecodeError."""
    from mediareaderx import decoders

    path = Path(path)
    decoders.image_kind(path.suffix)
    try:
        if not _pool.usable():
            return decoders.decode(path, max_side)
        op_timeout = TIMEOUTS["decode" if max_side else "decode_full"]
        image = _request("view", "decode", path, {"max_side": max_side}, op_timeout, _read_decoded)
    except Unavailable:
        if _pool.state == "fallback":
            return decoders.decode(path, max_side)
        raise DecodeError(UNAVAILABLE) from None
    except Busy:
        raise DecodeError(BUSY) from None
    try:
        image.metadata = file_items(path) + image.metadata
    except OSError as exc:
        raise DecodeError("The file cannot be read. It may have been moved or you may lack access.") from exc
    return image


def make_thumbnail(path: str | Path, side: int = 192):
    """``thumbnails.make_thumbnail``, protected: a QImage or None."""
    from mediareaderx import thumbnails

    path = Path(path)
    try:
        if not _pool.usable():
            return thumbnails.make_thumbnail(path, side)
        return _request("thumb", "thumb", path, {"side": side}, TIMEOUTS["thumb"], _read_thumb)
    except Unavailable:
        return thumbnails.make_thumbnail(path, side) if _pool.state == "fallback" else None
    except (DecodeError, Busy):
        return None


def _check_in_process(path: Path) -> str | None:
    from mediareaderx import content, videoprobe

    notice = content.check_video(path)
    try:
        with open(path, "rb") as f:
            head = f.read(content.HEAD_BYTES)
            size = os.fstat(f.fileno()).st_size
            demuxer = content.ffmpeg_demuxer(head, path.suffix.lower(), at_end=size <= len(head)) or ""
            f.seek(0)
            found = videoprobe.streams(f, size, demuxer)
    except (videoprobe.ProbeError, OSError):
        raise content.Refused(videoprobe.DAMAGED) from None
    if (reason := videoprobe.refusal(found)) is not None:
        raise content.Refused(reason)
    return notice


def _read_streams(header: dict) -> list[tuple[str, str]]:
    found = header.get("streams")
    if not isinstance(found, list) or len(found) > 64:
        raise protocol.ProtocolError("bad streams")
    out = []
    for item in found:
        if not isinstance(item, list) or len(item) != 2:
            raise protocol.ProtocolError("bad stream")
        out.append((protocol.clean_text(item[0], 16), protocol.clean_text(item[1], 32)))
    return out


def check_video(path: str | Path) -> str | None:
    """``content.check_video`` plus the codec list, read by a fresh helper. Returns a notice or None.
    Raises content.Refused with the user-facing reason."""
    from mediareaderx import content, videoprobe

    path = Path(path)
    notice = content.check_video(path)  # this process: known container, FFmpeg's probe agrees

    def read(header: dict, _f):
        if _blobs(header):
            raise protocol.ProtocolError("unexpected blobs")
        if header.get("refused") is not None:
            raise content.Refused(protocol.clean_text(header["refused"]))
        return _read_streams(header)

    try:
        if not _pool.usable():
            return _check_in_process(path)
        found = _request("check", "video", path, {"frame": False}, TIMEOUTS["video"], read)
    except Unavailable:
        if _pool.state == "fallback":
            return _check_in_process(path)
        raise content.Refused(UNAVAILABLE) from None
    except Busy:
        raise content.Refused(BUSY) from None
    except DecodeError as exc:
        raise content.Refused(str(exc)) from None
    if (reason := videoprobe.refusal(found)) is not None:
        raise content.Refused(reason)
    return notice


def video_thumbnail(path: str | Path, side: int = 192):
    """(frame or None, length in ms) for the thumbnail strip, from a helper. Raises UseInProcess when the
    strip should use its in-process thumbnailer instead (protected decoding off or never worked here)."""
    from mediareaderx import content, videoprobe

    path = Path(path)
    try:
        content.check_video(path)
    except content.Refused:
        return None, 0

    def read(header: dict, f):
        if header.get("refused") is not None:
            if _blobs(header):
                raise protocol.ProtocolError("unexpected blobs")
            return None, 0
        found = _read_streams(header)
        duration = header.get("duration", 0)
        if not protocol._is_int(duration, 0, 1 << 40):
            raise protocol.ProtocolError("bad duration")
        frames = _read_frames(header, f, 1, 4096 * 4096, 4096 * 4096 * 4)
        if videoprobe.refusal(found) is not None:
            return None, 0
        return (frames[0] if frames else None), duration

    try:
        if not _pool.usable():
            raise UseInProcess()
        return _request("thumb", "video", path, {"frame": True, "side": side}, TIMEOUTS["video"], read)
    except Unavailable:
        if _pool.state == "fallback":
            raise UseInProcess() from None
        return None, 0
    except (DecodeError, Busy):
        return None, 0


def in_process() -> bool:
    """True when files are decoded in this process (protected decoding off, or never worked on this PC)."""
    return _pool.state in ("off", "fallback")


def selfcheck(probe_path: Path) -> dict:
    """For --self-test: start a helper and let it try to read ``probe_path``, use the network and start a
    program. Returns what was blocked."""
    helper = _pool.acquire("view")
    keep = False
    try:
        protocol.write_message(helper.to_helper, {"op": "selfcheck", "path": str(probe_path)})
        header = protocol.read_header(helper.from_helper)
        keep = True
        return header.get("results", {})
    finally:
        _pool.release(helper, keep)


__all__ = ["decode", "make_thumbnail", "check_video", "video_thumbnail", "configure", "start", "shutdown",
           "state", "notifier", "Unavailable", "UseInProcess", "in_process", "remove_profiles", "selfcheck"]
