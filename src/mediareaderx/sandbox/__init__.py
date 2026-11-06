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
  In "fallback", a background attempt runs every ``FALLBACK_RETRY_S``; the first helper that starts turns
  protection on.
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
from mediareaderx.decoded import DecodedImage, DecodeError
from mediareaderx.metadata import MetaItem, file_items
from mediareaderx.sandbox import protocol

log = logging.getLogger(__name__)

CAPABILITY = "xavArtMediaReaderXFiles"  # helpers hold it; the app folder lets it read and execute
SLOT_PREFIX = "xav-art.MediaReaderX.Helper"
OLD_PROFILES = ("xav-art.MediaReaderX.Decoder",)  # the 1.6 prototype's profile name
SLOTS = 16  # shared by every running copy of the app: a slot is claimed with a per-user lock
READY_TIMEOUT_S = 90  # a first start on a PC whose antivirus scans every new process can be slow
GUI_READY_TIMEOUT_S = 25  # a start the window waits for (opening a video, copy at full size) is cut shorter
# Idle helpers beyond the spares exit after this long (memory goes back to the PC). Thumbnails come in bursts.
IDLE_EXIT_S = {"view": 60, "thumb": 30, "check": 120}
# A busy helper usually finishes a photo sooner than a new helper starts (about 1.5 s with warm codecs): wait
# this long for one before starting another. Fewer helpers, same speed (measured, see docs/SANDBOX.md).
SPAWN_AFTER_S = {"view": 0.6, "thumb": 0.4, "check": 0.0}
ACQUIRE_WAIT_S = 30  # longest wait for a free helper; then "busy" (the GUI thread never waits for minutes)
RETRY_S = 30
FALLBACK_RETRY_S = 120
GB = 1 << 30

# role: (most helpers at once, memory limit per helper, reused for more files)
ROLES = {"view": (2, 4 * GB, True), "thumb": (2, 1 * GB, True), "check": (3, 1 * GB, False)}
SPARES = {"view": 1, "thumb": 0, "check": 1}  # started ahead and kept ready, so a file does not wait
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
        self.station_handle = self.desktop_handle = None
        self.desktop_name = ""  # "station\\desktop" of the helpers' own window station
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
        self._station_locked(winapi, cap)
        self.cap = cap
        self.start_reaper()

    def start_reaper(self) -> None:
        if self.reaper is None:
            self.reaper = threading.Thread(target=self._reap, name="sandbox-reaper", daemon=True)
            self.reaper.start()

    def _station_locked(self, winapi, cap) -> None:
        if self.station_handle is None:
            self.station_handle, self.desktop_handle, self.desktop_name = winapi.private_station(
                cap, f"Helpers-{os.getpid()}")

    def prepare_station(self) -> None:
        """Make the helpers' window station now, on the GUI thread (it switches this process's station for a
        moment; doing it here means no other thread of the app makes a window meanwhile)."""
        from mediareaderx.sandbox import winapi

        with self.setup_lock:
            try:
                self._station_locked(winapi, winapi.capability(CAPABILITY))
            except OSError as exc:  # the first helper start tries again and reports it
                log.warning("Helper window station not ready: %s", exc)

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

    def _spawn(self, role: str, ready_timeout: float = READY_TIMEOUT_S) -> _Helper:
        """Start a helper and wait up to ``ready_timeout`` for its "ready". Raises OSError or protocol errors."""
        from mediareaderx.sandbox import winapi

        # Claim a slot no other running copy of the app holds: two live helpers never share a container.
        with self.cond:
            candidates = list(self.free_slots)
        slot = claim = None
        for candidate in candidates:
            claim = winapi.claim_slot(winapi.local_appdata() / "MediaReaderX" / "helper-slots" / f"{candidate}.lock")
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
            timer = threading.Timer(ready_timeout, helper.kill)
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
        PC before, or in this session), a failure fails closed, whatever caused it. While other helpers run,
        one helper that does not start changes nothing: those keep working."""
        log.warning("Protected decoder could not start: %s", exc)
        with self.cond:
            if self.state == "on" and any(h.proc for h in self.helpers):
                return
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
        start = time.monotonic()
        deadline = start + ACQUIRE_WAIT_S
        attempts = 0
        while True:
            if time.monotonic() > deadline:
                raise Busy()
            with self.cond:
                for helper in self.helpers:
                    if helper.role == role and not helper.busy and (ROLES[role][2] or not helper.served):
                        helper.busy = True
                        return helper
                live = sum(1 for h in self.helpers if h.role == role) + self.starting[role]
                patience = start + SPAWN_AFTER_S[role] - time.monotonic()
                if live and patience > 0:  # one is busy or starting: it is likely free before a new one starts
                    self.cond.wait(patience)
                    continue
                if (self.state == "new" and self.starting[role] and not self.worked_before
                        and threading.current_thread() is threading.main_thread()):
                    raise Busy()  # a first start is under way: the window does not start (and wait for) another
                if live < most and self.free_slots:
                    self.starting[role] += 1
                    generation = self.generation
                else:
                    self.cond.wait(1.0)
                    continue
            on_gui = threading.current_thread() is threading.main_thread()
            try:
                helper = self._spawn(role, GUI_READY_TIMEOUT_S if on_gui else READY_TIMEOUT_S)
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
                attempts += 1
                if self.state == "new" and attempts < 2 and not on_gui and time.monotonic() < deadline:
                    log.warning("Protected decoder could not start (%s): trying once more", exc)
                    continue
                if self.state == "new" and on_gui and not self.worked_before:
                    # The window waited 25 s; a first start can take longer on a PC whose antivirus scans it. Let
                    # the background start (two tries, 90 s each) decide; this file shows "still busy".
                    log.warning("Protected decoder slow to start (%s): continuing in the background", exc)
                    self.prewarm(role, want=1)
                    raise Busy() from exc
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

    def prewarm(self, role: str, want: int | None = None, retry: bool = False) -> None:
        """Start helpers in the background until ``want`` (default ``SPARES[role]``) are ready for a file.
        ``retry``: also in "fallback", to find out whether helpers work now."""
        want = SPARES[role] if want is None else want

        def run() -> None:
            try:
                if not retry and not self.usable():
                    return
            except Unavailable:
                return
            attempts = 0
            while True:
                with self.cond:
                    mine = [h for h in self.helpers if h.role == role]
                    ready = sum(1 for h in mine if not h.busy and (ROLES[role][2] or not h.served))
                    spares = ready + self.starting[role]
                    if (spares >= want or len(mine) + self.starting[role] >= ROLES[role][0]
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
                    if stale or isinstance(exc, NoSlot):
                        return
                    attempts += 1
                    if self.state == "new" and attempts < 2:
                        log.warning("Protected decoder could not start (%s): trying once more", exc)
                        continue
                    self._start_failed(exc)
                    return
                if not self._adopt(helper, role, generation, busy=False):
                    return
                if self.state != "on":
                    self._set_state("on")

        threading.Thread(target=run, name=f"sandbox-prewarm-{role}", daemon=True).start()

    def _reap(self) -> None:
        while True:
            time.sleep(10)
            now = time.monotonic()
            if self.state == "fallback" and now - self.failed_at > FALLBACK_RETRY_S:
                self.failed_at = now
                self.prewarm("view", want=1, retry=True)
            idle = []
            with self.cond:
                for role in ROLES:
                    mine = [h for h in self.helpers if h.role == role and not h.busy]
                    for h in mine[SPARES[role]:]:
                        if now - h.last_used > IDLE_EXIT_S[role]:
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
            winapi.close_station(self.station_handle, self.desktop_handle)
            winapi.close(self.outer_job)
            self.station_handle = self.desktop_handle = self.outer_job = self.cap = None
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
        _pool.start_reaper()
        _pool.prepare_station()
        _pool.prewarm("view")
        _pool.prewarm("check")


def shutdown() -> None:
    _pool.shutdown()


def warm_viewers() -> None:
    """The user browses a folder: keep two viewer helpers, so slow photos (HEIC, RAW: about 1 s each) preload in
    parallel. The second one exits after ``IDLE_EXIT_S["view"]`` without work."""
    if sys.platform == "win32" and _pool.state not in ("off", "fallback"):
        _pool.prewarm("view", want=ROLES["view"][0])


def warm_thumbnails() -> None:
    """A folder is about to fill the thumbnail strip: start a thumbnail helper now, alongside the photo's decode."""
    if sys.platform == "win32" and _pool.state not in ("off", "fallback"):
        _pool.prewarm("thumb", want=1)


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
        if isinstance(notes, list) and notes:  # their text stays out of the log: it can carry text from the file
            log.debug("Decoder warnings: %d", min(len(notes), 20))
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


def _read_decoded(header: dict, f, max_side: int | None = None) -> DecodedImage:
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
    if max_side and count == 1 and full is None:  # a still that was not reduced: SVG renders up to 4096 px
        spec = frames_spec[0]
        if (not isinstance(spec, list) or len(spec) != 3 or not protocol._is_int(spec[0], 1, max(max_side, 4096) + 2)
                or not protocol._is_int(spec[1], 1, max(max_side, 4096) + 2)):
            raise protocol.ProtocolError("picture larger than asked")
    if max_side and count == 1 and full is not None:  # a screen copy: never larger than asked
        spec = frames_spec[0]
        if (not isinstance(spec, list) or len(spec) != 3 or not protocol._is_int(spec[0], 1, max_side + 2)
                or not protocol._is_int(spec[1], 1, max_side + 2)):
            raise protocol.ProtocolError("screen copy larger than asked")
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


def _small_frames(header: dict, f, side: int) -> list:
    """At most one frame, no larger than ``side`` (+2 for rounding) on either side."""
    limit = side + 2
    frames = header.get("frames")
    if isinstance(frames, list) and frames:
        spec = frames[0]
        if (not isinstance(spec, list) or len(spec) != 3 or not protocol._is_int(spec[0], 1, limit)
                or not protocol._is_int(spec[1], 1, limit)):
            raise protocol.ProtocolError("thumbnail larger than asked")
    return _read_frames(header, f, 1, limit * limit, limit * limit * 4)


def _read_thumb(header: dict, f, side: int = 192):
    frames = _small_frames(header, f, side)
    return frames[0] if frames else None


# -- public API -------------------------------------------------------------------------------------------
def decode(path: str | Path, max_side: int | None = None) -> DecodedImage:
    """``decoders.decode``, protected. Raises DecodeError."""
    from mediareaderx.decoded import image_kind

    path = Path(path)
    image_kind(path.suffix)
    try:
        if not _pool.usable():
            from mediareaderx import decoders  # in this process only when protection is off

            return decoders.decode(path, max_side)
        op_timeout = TIMEOUTS["decode" if max_side else "decode_full"]
        image = _request("view", "decode", path, {"max_side": max_side}, op_timeout,
                         lambda header, f: _read_decoded(header, f, max_side))
    except Unavailable:
        if _pool.state == "fallback":
            from mediareaderx import decoders

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
    path = Path(path)

    def in_process():
        from mediareaderx import thumbnails  # numpy and the codecs load here only when protection is off

        return thumbnails.make_thumbnail(path, side)

    try:
        if not _pool.usable():
            return in_process()
        return _request("thumb", "thumb", path, {"side": side}, TIMEOUTS["thumb"],
                        lambda header, f: _read_thumb(header, f, side))
    except Unavailable:
        return in_process() if _pool.state == "fallback" else None
    except (DecodeError, Busy):
        return None


def check_video_in_process(path: str | Path, scan: bool = True) -> str | None:
    """``check_video`` without a helper (protection off or never worked): container and codec list. ``scan``
    reads on for late streams (before playback; thumbnails skip it)."""
    from mediareaderx import content, videoprobe

    path = Path(path)
    notice = content.check_video(path)
    try:
        with open(path, "rb") as f:
            head = f.read(content.HEAD_BYTES)
            size = os.fstat(f.fileno()).st_size
            demuxer = content.ffmpeg_demuxer(head, path.suffix.lower(), at_end=size <= len(head)) or ""
            f.seek(0)
            found = videoprobe.streams(f, size, demuxer,
                                       scan_bytes=videoprobe.scan_bytes_for(demuxer) if scan else 0)
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
            return check_video_in_process(path)
        found = _request("check", "video", path, {"frame": False}, TIMEOUTS["video"], read)
    except Unavailable:
        if _pool.state == "fallback":
            return check_video_in_process(path)
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
    from mediareaderx import videoprobe

    path = Path(path)  # no FFmpeg probe here: the helper checks the container before anything else

    def read(header: dict, f):
        if header.get("refused") is not None:
            if _blobs(header):
                raise protocol.ProtocolError("unexpected blobs")
            return None, 0
        found = _read_streams(header)
        duration = header.get("duration", 0)
        if not protocol._is_int(duration, 0, 1 << 40):
            raise protocol.ProtocolError("bad duration")
        frames = _small_frames(header, f, side)
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


__all__ = ["decode", "make_thumbnail", "check_video", "check_video_in_process", "video_thumbnail", "configure",
           "start", "shutdown",
           "state", "notifier", "Unavailable", "UseInProcess", "in_process", "remove_profiles", "selfcheck"]
