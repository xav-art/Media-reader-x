"""Windows plumbing for protected decoding (see docs/SANDBOX.md).

AppContainer profiles (one per helper slot), a custom capability that may read the app's own files,
job objects, a private desktop, and starting a helper with exactly the handles it needs. No admin
rights are needed for any of it. Every call checks its result: a failure raises OSError, and the caller
then refuses to decode rather than start a helper with weaker settings.
"""

from __future__ import annotations

import ctypes
import functools
import logging
import os
import secrets
from ctypes import wintypes as wt
from pathlib import Path

log = logging.getLogger(__name__)

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
kbase = ctypes.WinDLL("kernelbase", use_last_error=True)
advapi = ctypes.WinDLL("advapi32", use_last_error=True)
userenv = ctypes.WinDLL("userenv", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

PSID = ctypes.c_void_p
HANDLE = wt.HANDLE
INVALID_HANDLE = ctypes.c_void_p(-1).value


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", PSID), ("Attributes", wt.DWORD)]


class SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [("AppContainerSid", PSID), ("Capabilities", ctypes.POINTER(SID_AND_ATTRIBUTES)),
                ("CapabilityCount", wt.DWORD), ("Reserved", wt.DWORD)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR), ("lpTitle", wt.LPWSTR),
                ("dwX", wt.DWORD), ("dwY", wt.DWORD), ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
                ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD),
                ("dwFlags", wt.DWORD), ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", HANDLE), ("hStdOutput", HANDLE),
                ("hStdError", HANDLE)]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE), ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wt.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", wt.BOOL)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class TRUSTEE_W(ctypes.Structure):
    _fields_ = [("pMultipleTrustee", ctypes.c_void_p), ("MultipleTrusteeOperation", ctypes.c_int),
                ("TrusteeForm", ctypes.c_int), ("TrusteeType", ctypes.c_int), ("ptstrName", ctypes.c_void_p)]


class EXPLICIT_ACCESS_W(ctypes.Structure):
    _fields_ = [("grfAccessPermissions", wt.DWORD), ("grfAccessMode", ctypes.c_int),
                ("grfInheritance", wt.DWORD), ("Trustee", TRUSTEE_W)]


class ACE_HEADER(ctypes.Structure):
    _fields_ = [("AceType", ctypes.c_ubyte), ("AceFlags", ctypes.c_ubyte), ("AceSize", wt.WORD)]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD), ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def _sig(dll, name, restype, *argtypes):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = restype, list(argtypes)
    return fn


_CreateAppContainerProfile = _sig(userenv, "CreateAppContainerProfile", ctypes.c_long, wt.LPCWSTR, wt.LPCWSTR,
                                  wt.LPCWSTR, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(PSID))
_DeleteAppContainerProfile = _sig(userenv, "DeleteAppContainerProfile", ctypes.c_long, wt.LPCWSTR)
_DeriveSid = _sig(userenv, "DeriveAppContainerSidFromAppContainerName", ctypes.c_long, wt.LPCWSTR,
                  ctypes.POINTER(PSID))
_GetFolder = _sig(userenv, "GetAppContainerFolderPath", ctypes.c_long, wt.LPCWSTR, ctypes.POINTER(wt.LPWSTR))
_DeriveCapability = _sig(kbase, "DeriveCapabilitySidsFromName", wt.BOOL, wt.LPCWSTR,
                         ctypes.POINTER(ctypes.POINTER(PSID)), ctypes.POINTER(wt.DWORD),
                         ctypes.POINTER(ctypes.POINTER(PSID)), ctypes.POINTER(wt.DWORD))
_SidToString = _sig(advapi, "ConvertSidToStringSidW", wt.BOOL, PSID, ctypes.POINTER(wt.LPWSTR))
_EqualSid = _sig(advapi, "EqualSid", wt.BOOL, PSID, PSID)
_GetLengthSid = _sig(advapi, "GetLengthSid", wt.DWORD, PSID)
_GetNamedSecurityInfo = _sig(advapi, "GetNamedSecurityInfoW", wt.DWORD, wt.LPCWSTR, ctypes.c_int, wt.DWORD,
                             ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                             ctypes.POINTER(ctypes.c_void_p))
_SetNamedSecurityInfo = _sig(advapi, "SetNamedSecurityInfoW", wt.DWORD, wt.LPWSTR, ctypes.c_int, wt.DWORD,
                             ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
_SetEntriesInAcl = _sig(advapi, "SetEntriesInAclW", wt.DWORD, wt.ULONG, ctypes.POINTER(EXPLICIT_ACCESS_W),
                        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
_GetAce = _sig(advapi, "GetAce", wt.BOOL, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(ctypes.c_void_p))
_ConvertSddl = _sig(advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW", wt.BOOL, wt.LPCWSTR, wt.DWORD,
                    ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p)
_OpenProcessToken = _sig(advapi, "OpenProcessToken", wt.BOOL, HANDLE, wt.DWORD, ctypes.POINTER(HANDLE))
_GetTokenInformation = _sig(advapi, "GetTokenInformation", wt.BOOL, HANDLE, ctypes.c_int, ctypes.c_void_p,
                            wt.DWORD, ctypes.POINTER(wt.DWORD))
_LocalFree = _sig(k32, "LocalFree", ctypes.c_void_p, ctypes.c_void_p)
_CloseHandle = _sig(k32, "CloseHandle", wt.BOOL, HANDLE)
_GetCurrentProcess = _sig(k32, "GetCurrentProcess", HANDLE)
_CreatePipe = _sig(k32, "CreatePipe", wt.BOOL, ctypes.POINTER(HANDLE), ctypes.POINTER(HANDLE),
                   ctypes.POINTER(SECURITY_ATTRIBUTES), wt.DWORD)
_SetHandleInformation = _sig(k32, "SetHandleInformation", wt.BOOL, HANDLE, wt.DWORD, wt.DWORD)
_InitAttr = _sig(k32, "InitializeProcThreadAttributeList", wt.BOOL, ctypes.c_void_p, wt.DWORD, wt.DWORD,
                 ctypes.POINTER(ctypes.c_size_t))
_UpdateAttr = _sig(k32, "UpdateProcThreadAttribute", wt.BOOL, ctypes.c_void_p, wt.DWORD, ctypes.c_size_t,
                   ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p)
_DeleteAttr = _sig(k32, "DeleteProcThreadAttributeList", None, ctypes.c_void_p)
_CreateProcess = _sig(k32, "CreateProcessW", wt.BOOL, wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                      wt.BOOL, wt.DWORD, ctypes.c_void_p, wt.LPCWSTR, ctypes.c_void_p,
                      ctypes.POINTER(PROCESS_INFORMATION))
_CreateJob = _sig(k32, "CreateJobObjectW", HANDLE, ctypes.c_void_p, wt.LPCWSTR)
_SetJob = _sig(k32, "SetInformationJobObject", wt.BOOL, HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD)
_QueryJob = _sig(k32, "QueryInformationJobObject", wt.BOOL, HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD,
                 ctypes.POINTER(wt.DWORD))
_Terminate = _sig(k32, "TerminateProcess", wt.BOOL, HANDLE, wt.UINT)
_Wait = _sig(k32, "WaitForSingleObject", wt.DWORD, HANDLE, wt.DWORD)
_ExitCode = _sig(k32, "GetExitCodeProcess", wt.BOOL, HANDLE, ctypes.POINTER(wt.DWORD))
_Duplicate = _sig(k32, "DuplicateHandle", wt.BOOL, HANDLE, HANDLE, HANDLE, ctypes.POINTER(HANDLE), wt.DWORD,
                  wt.BOOL, wt.DWORD)
_CreateFile = _sig(k32, "CreateFileW", HANDLE, wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD,
                   HANDLE)
_GetFileType = _sig(k32, "GetFileType", wt.DWORD, HANDLE)
_GetFileSizeEx = _sig(k32, "GetFileSizeEx", wt.BOOL, HANDLE, ctypes.POINTER(ctypes.c_int64))
_ReadFile = _sig(k32, "ReadFile", wt.BOOL, HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
                 ctypes.c_void_p)
_SetFilePointerEx = _sig(k32, "SetFilePointerEx", wt.BOOL, HANDLE, ctypes.c_int64, ctypes.POINTER(ctypes.c_int64),
                         wt.DWORD)
_MemoryStatus = _sig(k32, "GlobalMemoryStatusEx", wt.BOOL, ctypes.POINTER(MEMORYSTATUSEX))
_CreateDesktop = _sig(user32, "CreateDesktopW", HANDLE, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_void_p, wt.DWORD,
                      wt.DWORD, ctypes.POINTER(SECURITY_ATTRIBUTES))
_CloseDesktop = _sig(user32, "CloseDesktop", wt.BOOL, HANDLE)


_FreeSid = _sig(advapi, "FreeSid", ctypes.c_void_p, PSID)


def _p(sid) -> ctypes.c_void_p:
    """A SID argument: our copies are byte arrays, the API's own are pointers."""
    return ctypes.cast(sid, ctypes.c_void_p) if isinstance(sid, ctypes.Array) else sid


def _check(ok, what: str):
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error(), what)
    return ok


def _hresult(hr: int, what: str) -> None:
    if hr != 0:
        raise OSError(f"{what} failed: 0x{hr & 0xFFFFFFFF:08x}")


HRESULT_ALREADY_EXISTS = ctypes.c_long(0x800700B7).value


# -- SIDs ------------------------------------------------------------------------------------------------
def sid_string(sid: PSID) -> str:
    out = wt.LPWSTR()
    _check(_SidToString(_p(sid), ctypes.byref(out)), "ConvertSidToStringSid")
    try:
        return out.value
    finally:
        _LocalFree(out)


def _copy_sid(sid: PSID) -> ctypes.Array:
    """A Python-owned copy of a SID (the API's copy is freed by its own rules)."""
    size = _GetLengthSid(sid)
    buf = (ctypes.c_ubyte * size)()
    ctypes.memmove(buf, sid, size)
    return buf


def fresh_container(name: str) -> ctypes.Array:
    """Delete and re-create the AppContainer profile ``name``: a new, empty folder for each helper launch,
    so nothing an earlier helper wrote there survives. Returns the container SID (it depends on the name only)."""
    _DeleteAppContainerProfile(name)  # nothing to delete is fine
    sid = PSID()
    hr = _CreateAppContainerProfile(name, name, "Media Reader x protected decoder", None, 0, ctypes.byref(sid))
    if hr == HRESULT_ALREADY_EXISTS:  # the folder could not be removed (a file still open): reuse the profile
        hr = _DeriveSid(name, ctypes.byref(sid))
    _hresult(hr, "AppContainer profile")
    try:
        return _copy_sid(sid)
    finally:
        _FreeSid(sid)


def delete_container(name: str) -> None:
    _DeleteAppContainerProfile(name)


def container_folder(sid) -> str:
    out = wt.LPWSTR()
    _hresult(_GetFolder(sid_string(sid), ctypes.byref(out)), "AppContainer folder")
    try:
        return out.value
    finally:
        ctypes.windll.ole32.CoTaskMemFree(out)


@functools.cache
def capability(name: str) -> ctypes.Array:
    """The SID of a custom capability. Helpers hold it; the app folder grants it read and execute."""
    groups, ngroups, caps, ncaps = ctypes.POINTER(PSID)(), wt.DWORD(), ctypes.POINTER(PSID)(), wt.DWORD()
    _check(_DeriveCapability(name, ctypes.byref(groups), ctypes.byref(ngroups), ctypes.byref(caps),
                             ctypes.byref(ncaps)), "DeriveCapabilitySidsFromName")
    if ncaps.value < 1:
        raise OSError("no capability SID")
    sid = _copy_sid(caps[0])
    for arr, n in ((groups, ngroups.value), (caps, ncaps.value)):
        for i in range(n):
            _LocalFree(arr[i])
        _LocalFree(arr)
    return sid


@functools.cache
def user_sid_string() -> str:
    token = HANDLE()
    _check(_OpenProcessToken(_GetCurrentProcess(), 0x0008, ctypes.byref(token)), "OpenProcessToken")  # TOKEN_QUERY
    try:
        need = wt.DWORD()
        _GetTokenInformation(token, 1, None, 0, ctypes.byref(need))  # TokenUser
        buf = ctypes.create_string_buffer(need.value)
        _check(_GetTokenInformation(token, 1, buf, need, ctypes.byref(need)), "GetTokenInformation")
        return sid_string(SID_AND_ATTRIBUTES.from_buffer(buf).Sid)
    finally:
        _CloseHandle(token)


# -- File access for the capability ---------------------------------------------------------------------
SE_FILE_OBJECT, DACL_SECURITY_INFORMATION = 1, 4
ACCESS_ALLOWED_ACE_TYPE = 0
FILE_GENERIC_READ_EXECUTE = 0x001200A9  # FILE_GENERIC_READ | FILE_EXECUTE
GRANT_ACCESS, SUB_CONTAINERS_AND_OBJECTS_INHERIT, TRUSTEE_IS_SID, TRUSTEE_IS_WELL_KNOWN_GROUP = 1, 3, 0, 5


def has_read_access(path: Path, sid) -> bool:
    """True when ``path``'s own DACL already has an allow entry for ``sid`` (granted earlier)."""
    dacl, sd = ctypes.c_void_p(), ctypes.c_void_p()
    err = _GetNamedSecurityInfo(str(path), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION, None, None,
                                ctypes.byref(dacl), None, ctypes.byref(sd))
    if err:
        raise ctypes.WinError(err, "GetNamedSecurityInfo")
    try:
        if not dacl.value:
            return False
        count = ctypes.c_ushort.from_address(dacl.value + 4).value  # ACL.AceCount
        for i in range(count):
            ace = ctypes.c_void_p()
            if not _GetAce(dacl, i, ctypes.byref(ace)):
                continue
            header = ACE_HEADER.from_address(ace.value)
            if header.AceType != ACCESS_ALLOWED_ACE_TYPE:
                continue
            mask = wt.DWORD.from_address(ace.value + 4).value
            if _EqualSid(ace.value + 8, _p(sid)) and mask & FILE_GENERIC_READ_EXECUTE == FILE_GENERIC_READ_EXECUTE:
                return True
        return False
    finally:
        _LocalFree(sd)


def grant_read(path: Path, sid) -> None:
    """Let ``sid`` read and run ``path`` and everything below it (inherited). Changes nothing else."""
    dacl, sd = ctypes.c_void_p(), ctypes.c_void_p()
    err = _GetNamedSecurityInfo(str(path), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION, None, None,
                                ctypes.byref(dacl), None, ctypes.byref(sd))
    if err:
        raise ctypes.WinError(err, "GetNamedSecurityInfo")
    new = ctypes.c_void_p()
    try:
        entry = EXPLICIT_ACCESS_W(FILE_GENERIC_READ_EXECUTE, GRANT_ACCESS, SUB_CONTAINERS_AND_OBJECTS_INHERIT,
                                  TRUSTEE_W(None, 0, TRUSTEE_IS_SID, TRUSTEE_IS_WELL_KNOWN_GROUP,
                                            ctypes.cast(sid, ctypes.c_void_p)))
        err = _SetEntriesInAcl(1, ctypes.byref(entry), dacl, ctypes.byref(new))
        if err:
            raise ctypes.WinError(err, "SetEntriesInAcl")
        err = _SetNamedSecurityInfo(str(path), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION, None, None, new, None)
        if err:
            raise ctypes.WinError(err, "SetNamedSecurityInfo")
    finally:
        _LocalFree(sd)
        if new.value:
            _LocalFree(new)


# -- Private desktop -----------------------------------------------------------------------------------
GENERIC_ALL = 0x10000000


_CreateWindowStation = _sig(user32, "CreateWindowStationW", HANDLE, wt.LPCWSTR, wt.DWORD, wt.DWORD,
                           ctypes.POINTER(SECURITY_ATTRIBUTES))
_GetProcessWindowStation = _sig(user32, "GetProcessWindowStation", HANDLE)
_SetProcessWindowStation = _sig(user32, "SetProcessWindowStation", wt.BOOL, HANDLE)
_CloseWindowStation = _sig(user32, "CloseWindowStation", wt.BOOL, HANDLE)
_GetUserObjectInformation = _sig(user32, "GetUserObjectInformationW", wt.BOOL, HANDLE, ctypes.c_int, ctypes.c_void_p,
                                 wt.DWORD, ctypes.POINTER(wt.DWORD))
WINSTA_ALL_ACCESS, UOI_NAME, CWF_CREATE_ONLY = 0x37F, 2, 0x1
READ_CONTROL, WRITE_DAC = 0x20000, 0x40000
_station_lock = __import__("threading").Lock()


_GetSecurityInfo = _sig(advapi, "GetSecurityInfo", wt.DWORD, HANDLE, ctypes.c_int, wt.DWORD, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                       ctypes.POINTER(ctypes.c_void_p))
_SetSecurityInfo = _sig(advapi, "SetSecurityInfo", wt.DWORD, HANDLE, ctypes.c_int, wt.DWORD, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
SE_WINDOW_OBJECT = 7


def _grant_object(handle: int, sid, access: int) -> None:
    """Add an allow entry for ``sid`` to a window station's or desktop's own access list."""
    dacl, sd = ctypes.c_void_p(), ctypes.c_void_p()
    err = _GetSecurityInfo(handle, SE_WINDOW_OBJECT, DACL_SECURITY_INFORMATION, None, None, ctypes.byref(dacl),
                           None, ctypes.byref(sd))
    if err:
        raise ctypes.WinError(err, "GetSecurityInfo")
    new = ctypes.c_void_p()
    try:
        entry = EXPLICIT_ACCESS_W(access, GRANT_ACCESS, 0, TRUSTEE_W(None, 0, TRUSTEE_IS_SID,
                                                                    TRUSTEE_IS_WELL_KNOWN_GROUP,
                                                                    ctypes.cast(sid, ctypes.c_void_p)))
        err = _SetEntriesInAcl(1, ctypes.byref(entry), dacl, ctypes.byref(new))
        if err:
            raise ctypes.WinError(err, "SetEntriesInAcl")
        err = _SetSecurityInfo(handle, SE_WINDOW_OBJECT, DACL_SECURITY_INFORMATION, None, None, new, None)
        if err:
            raise ctypes.WinError(err, "SetSecurityInfo")
    finally:
        _LocalFree(sd)
        if new.value:
            _LocalFree(new)


def private_station(cap, desktop_name: str) -> tuple[int, int, str]:
    """An empty desktop for the helpers, outside the user's window station: (station, desktop,
    "station\\desktop").

    A desktop on WinSta0 is not enough: an AppContainer process may open the interactive desktop
    (WinSta0\\Default) by name and capture the screen (verified 2026-11-05). From another window station it
    cannot open WinSta0 at all (access denied, verified).

    First choice: a named station of this process's own, newly created (CWF_CREATE_ONLY, a random name), that
    only the user, the capability (station rights and READ_CONTROL), SYSTEM and its owner can open. Where
    creating it fails (seen on this PC; the log says which station is used), the fallback is the station Windows
    gives an
    unnamed request, as Chromium's sandbox does: the logon session's service station, shared by every running
    copy; the capability gets access to it.
    The unnamed station does not work for an elevated built-in Administrator (GitHub's Windows runner: user32
    fails in the helper, verified 2026-11-06); the named one does. The desktop is always this process's own.
    Creating the desktop needs the station as this process's station for a moment. Keep both handles open
    while helpers run."""
    user, capability = user_sid_string(), sid_string(cap)
    sddl = f"D:P(A;;GA;;;{user})(A;;GA;;;{capability})(A;;GA;;;SY)"
    # The station's own list: the capability gets the station rights and READ_CONTROL (0x2037F; user32 in the
    # helper needs it, verified on GitHub's runner), never WRITE_DAC, WRITE_OWNER or DELETE, so a taken-over
    # helper cannot open the station to others.
    station_sddl = f"D:P(A;;GA;;;{user})(A;;0x2037F;;;{capability})(A;;GA;;;SY)"
    sd, station_sd = ctypes.c_void_p(), ctypes.c_void_p()
    _check(_ConvertSddl(sddl, 1, ctypes.byref(sd), None), "ConvertStringSecurityDescriptor")
    try:
        _check(_ConvertSddl(station_sddl, 1, ctypes.byref(station_sd), None), "ConvertStringSecurityDescriptor")
        sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), sd, False)
        station_sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), station_sd, False)
        own_name = f"MediaReaderX-{os.getpid()}-{secrets.token_hex(8)}"
        station = _CreateWindowStation(own_name, CWF_CREATE_ONLY, WINSTA_ALL_ACCESS, ctypes.byref(station_sa))
        can_grant = False  # our own access list already has the capability
        if station:
            log.info("Helper window station: own named station")
        else:
            log.info("Helper window station: own named station refused (error %d), using the session's service "
                     "station", ctypes.get_last_error())
            # Windows' default access list for the station: other programs' sandboxes (Chrome, Edge) use it too.
            station = _CreateWindowStation(None, 0, WINSTA_ALL_ACCESS | READ_CONTROL | WRITE_DAC, None)
            can_grant = bool(station)
        if not station:  # an existing station may refuse changes to its access list: use it as it is
            station = _check(_CreateWindowStation(None, 0, WINSTA_ALL_ACCESS, None), "CreateWindowStation")
        desktop = None
        try:
            if can_grant:
                _grant_object(station, cap, WINSTA_ALL_ACCESS)  # it may exist already, with another access list
            name = ctypes.create_unicode_buffer(256)
            _check(_GetUserObjectInformation(station, UOI_NAME, name, ctypes.sizeof(name), None), "station name")
            with _station_lock:
                previous = _GetProcessWindowStation()
                _check(_SetProcessWindowStation(station), "SetProcessWindowStation")
                try:
                    desktop = _CreateDesktop(desktop_name, None, None, 0, GENERIC_ALL, ctypes.byref(sa))
                finally:
                    # Back at once: the app's windows live on WinSta0.
                    _check(_SetProcessWindowStation(previous), "restore the window station")
            _check(desktop, "CreateDesktop")
        except BaseException:
            close_station(station, desktop)
            raise
        return station, desktop, f"{name.value}\\{desktop_name}"
    finally:
        _LocalFree(sd)
        if station_sd.value:
            _LocalFree(station_sd)


def close_station(station: int | None, desktop: int | None) -> None:
    if desktop:
        _CloseDesktop(desktop)
    if station:
        _CloseWindowStation(station)


# -- Jobs ------------------------------------------------------------------------------------------------
JOB_KILL_ON_CLOSE, JOB_DIE_ON_UNHANDLED, JOB_ACTIVE_PROCESS = 0x2000, 0x400, 0x8
JOB_PROCESS_MEMORY, JOB_JOB_MEMORY = 0x100, 0x200
# No other windows, clipboard, desktop switching, display or system settings, global atoms, logoff.
JOB_UI_ALL = 0x1 | 0x2 | 0x4 | 0x8 | 0x10 | 0x20 | 0x40 | 0x80


def helpers_job(total_bytes: int) -> int:
    """The job every helper runs in: killed with the app, at most ``total_bytes`` for all helpers together."""
    job = _check(_CreateJob(None, None), "CreateJobObject")
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_KILL_ON_CLOSE | JOB_DIE_ON_UNHANDLED | JOB_JOB_MEMORY
    info.JobMemoryLimit = total_bytes
    _check(_SetJob(job, 9, ctypes.byref(info), ctypes.sizeof(info)), "job limits")
    ui = wt.DWORD(JOB_UI_ALL)
    _check(_SetJob(job, 4, ctypes.byref(ui), ctypes.sizeof(ui)), "job UI limits")
    return job


def helper_job(process_bytes: int) -> int:
    """One helper's own job (nested in ``helpers_job``): one process, its own memory limit."""
    job = _check(_CreateJob(None, None), "CreateJobObject")
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = (JOB_KILL_ON_CLOSE | JOB_DIE_ON_UNHANDLED | JOB_ACTIVE_PROCESS
                                             | JOB_PROCESS_MEMORY)
    info.BasicLimitInformation.ActiveProcessLimit = 1
    info.ProcessMemoryLimit = process_bytes
    _check(_SetJob(job, 9, ctypes.byref(info), ctypes.sizeof(info)), "job limits")
    return job


def job_peak_memory(job: int) -> int:
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    if not _QueryJob(job, 9, ctypes.byref(info), ctypes.sizeof(info), None):
        return 0
    return info.PeakProcessMemoryUsed


def physical_memory() -> int:
    status = MEMORYSTATUSEX(ctypes.sizeof(MEMORYSTATUSEX))
    _check(_MemoryStatus(ctypes.byref(status)), "GlobalMemoryStatusEx")
    return status.ullTotalPhys


# -- Pipes and handles ----------------------------------------------------------------------------------
HANDLE_FLAG_INHERIT = 1


def pipe(child_reads: bool) -> tuple[int, int]:
    """An anonymous pipe as (read end, write end). Only the child's end is inheritable (and only listed
    handles reach a helper anyway), so no other process the app starts can inherit the parent's end."""
    sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, True)
    r, w = HANDLE(), HANDLE()
    _check(_CreatePipe(ctypes.byref(r), ctypes.byref(w), ctypes.byref(sa), 1 << 20), "CreatePipe")
    parent_end = w if child_reads else r
    _check(_SetHandleInformation(parent_end, HANDLE_FLAG_INHERIT, 0), "SetHandleInformation")
    return r.value, w.value


def close(handle: int | None) -> None:
    if handle:
        _CloseHandle(handle)


GENERIC_READ, OPEN_EXISTING, FILE_TYPE_DISK = 0x80000000, 3, 1
SHARE_ALL = 0x1 | 0x2 | 0x4  # read, write, delete: Delete and other apps still work while a helper reads


def open_for_read(path: Path, prefetch: int = 0) -> tuple[int, int, bytes]:
    """Open a regular file for reading: (handle, size, its first ``prefetch`` bytes). The read also makes a
    cloud-only OneDrive file download here, in the app, not in the helper. Raises OSError."""
    h = _CreateFile(str(path), GENERIC_READ, SHARE_ALL, None, OPEN_EXISTING, 0, None)
    if not h or h == INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error(), "open")
    try:
        if _GetFileType(h) != FILE_TYPE_DISK:
            raise OSError("not a file")
        size = ctypes.c_int64()
        _check(_GetFileSizeEx(h, ctypes.byref(size)), "GetFileSizeEx")
        head = b""
        if prefetch:
            buf = ctypes.create_string_buffer(prefetch)
            got = wt.DWORD()
            _check(_ReadFile(h, buf, prefetch, ctypes.byref(got), None), "ReadFile")
            head = buf.raw[:got.value]
            _check(_SetFilePointerEx(h, 0, None, 0), "SetFilePointerEx")
        return h, size.value, head
    except BaseException:
        _CloseHandle(h)
        raise


DUPLICATE_SAME_ACCESS, DUPLICATE_CLOSE_SOURCE = 0x2, 0x1


def give_handle(process: int, handle: int) -> int:
    """Move ``handle`` into ``process`` (closed here either way). Returns its value in that process."""
    remote = HANDLE()
    ok = _Duplicate(_GetCurrentProcess(), handle, process, ctypes.byref(remote), 0, False,
                    DUPLICATE_SAME_ACCESS | DUPLICATE_CLOSE_SOURCE)
    _check(ok, "DuplicateHandle")
    return remote.value


# -- Slot claims -----------------------------------------------------------------------------------------
ERROR_SHARING_VIOLATION = 32
FILE_FLAG_DELETE_ON_CLOSE = 0x04000000
_SHGetKnownFolderPath = _sig(ctypes.WinDLL("shell32"), "SHGetKnownFolderPath", ctypes.c_long, ctypes.c_void_p, wt.DWORD,
                             HANDLE, ctypes.POINTER(wt.LPWSTR))


class _GUID(ctypes.Structure):
    _fields_ = [("a", wt.DWORD), ("b", wt.WORD), ("c", wt.WORD), ("d", ctypes.c_ubyte * 8)]


def local_appdata() -> Path:
    """The real %LOCALAPPDATA% of this user, from Windows, not from the environment."""
    guid = _GUID(0xF1B32785, 0x6FBA, 0x4FCF, (ctypes.c_ubyte * 8)(0x9D, 0x55, 0x7B, 0x8E, 0x7F, 0x15, 0x70, 0x91))
    out = wt.LPWSTR()
    _hresult(_SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)), "SHGetKnownFolderPath")
    try:
        return Path(out.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(out)


def claim_slot(path: Path) -> int | None:
    """Hold the lock file ``path`` (in this user's own folder, so no other user can take it first): the handle
    when this process now holds it, None when another running copy does. Windows deletes the file and frees the
    lock when the handle closes, also when the process crashes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = _CreateFile(str(path), GENERIC_READ | 0x40000000, 0, None, 4,  # GENERIC_WRITE, no sharing, OPEN_ALWAYS
                         FILE_FLAG_DELETE_ON_CLOSE, None)
    if handle and handle != INVALID_HANDLE:
        return handle
    err = ctypes.get_last_error()
    if err in (ERROR_SHARING_VIOLATION, 5):  # held by another copy, or being deleted as its holder closes it
        return None
    raise ctypes.WinError(err, "slot lock")


# -- Processes -------------------------------------------------------------------------------------------
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
PROC_THREAD_ATTRIBUTE_MITIGATION_POLICY = 0x00020007
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
PROC_THREAD_ATTRIBUTE_CHILD_PROCESS_POLICY = 0x0002000E
EXTENDED_STARTUPINFO_PRESENT, DETACHED_PROCESS, CREATE_UNICODE_ENVIRONMENT = 0x00080000, 0x8, 0x400
PROCESS_CREATION_CHILD_PROCESS_RESTRICTED = 0x1
SE_GROUP_ENABLED = 0x4
# Exploit mitigations (PROCESS_CREATION_MITIGATION_POLICY_*_ALWAYS_ON). IMAGE_LOAD_NO_LOW_LABEL stops a DLL
# that a helper wrote into its own folder from loading (verified); PREFER_SYSTEM32 and the app folder as the
# working folder keep the DLL search order away from writable places.
MITIGATIONS = ((1 << 12)     # terminate on heap corruption
               | (1 << 16)   # bottom-up ASLR
               | (1 << 20)   # high-entropy ASLR
               | (1 << 24)   # strict handle checks
               | (1 << 32)   # no legacy extension points (AppInit DLLs, hooks)
               | (1 << 48)   # system fonts only
               | (1 << 52)   # no DLLs from network shares
               | (1 << 56)   # no DLLs with a low integrity label
               | (1 << 60))  # system DLLs from System32 first


def launch(container_sid, cap_sid, exe: str, args: list[str], env: dict[str, str], handles: list[int], cwd: str,
           desktop: str, jobs: list[int]) -> tuple[int, int]:
    """Start ``exe`` in the AppContainer with the capability, inside ``jobs`` (outer first), on ``desktop``,
    inheriting only ``handles``, unable to start processes. Returns (process handle, process id)."""
    from subprocess import list2cmdline

    n_attr = 5
    size = ctypes.c_size_t()
    _InitAttr(None, n_attr, 0, ctypes.byref(size))
    attrs = ctypes.create_string_buffer(size.value)
    _check(_InitAttr(attrs, n_attr, 0, ctypes.byref(size)), "InitializeProcThreadAttributeList")
    try:
        caps = (SID_AND_ATTRIBUTES * 1)(SID_AND_ATTRIBUTES(ctypes.cast(cap_sid, PSID), SE_GROUP_ENABLED))
        security = SECURITY_CAPABILITIES(ctypes.cast(container_sid, PSID), caps, 1, 0)
        harr = (HANDLE * len(handles))(*handles)
        jarr = (HANDLE * len(jobs))(*jobs)
        child = wt.DWORD(PROCESS_CREATION_CHILD_PROCESS_RESTRICTED)
        mitigations = ctypes.c_uint64(MITIGATIONS)
        for attr, value, length in (
                (PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES, ctypes.byref(security), ctypes.sizeof(security)),
                (PROC_THREAD_ATTRIBUTE_HANDLE_LIST, harr, ctypes.sizeof(harr)),
                (PROC_THREAD_ATTRIBUTE_JOB_LIST, jarr, ctypes.sizeof(jarr)),
                (PROC_THREAD_ATTRIBUTE_CHILD_PROCESS_POLICY, ctypes.byref(child), ctypes.sizeof(child)),
                (PROC_THREAD_ATTRIBUTE_MITIGATION_POLICY, ctypes.byref(mitigations), ctypes.sizeof(mitigations))):
            _check(_UpdateAttr(attrs, 0, attr, value, length, None, None), f"attribute 0x{attr:x}")
        si = STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        desk = ctypes.create_unicode_buffer(desktop)  # "station\\desktop" from private_station
        si.StartupInfo.lpDesktop = ctypes.cast(desk, wt.LPWSTR)
        si.lpAttributeList = ctypes.cast(attrs, ctypes.c_void_p)
        # Windows wants the block sorted by name; it remaps LOCALAPPDATA, TEMP and TMP to the container folder.
        block = "".join(f"{k}={v}\0" for k, v in sorted(env.items(), key=lambda kv: kv[0].upper())) + "\0"
        envbuf = ctypes.create_unicode_buffer(block, len(block))
        cmd = ctypes.create_unicode_buffer(list2cmdline([exe, *args]))
        pi = PROCESS_INFORMATION()
        _check(_CreateProcess(exe, cmd, None, None, True,
                              EXTENDED_STARTUPINFO_PRESENT | DETACHED_PROCESS | CREATE_UNICODE_ENVIRONMENT,
                              envbuf, cwd, ctypes.byref(si), ctypes.byref(pi)), "CreateProcess")
        _CloseHandle(pi.hThread)
        return pi.hProcess, pi.dwProcessId
    finally:
        _DeleteAttr(attrs)


def terminate(process: int) -> None:
    _Terminate(process, 1)


def wait(process: int, ms: int) -> bool:
    """True when the process has exited within ``ms``."""
    return _Wait(process, ms) == 0


def exit_code(process: int) -> int | None:
    code = wt.DWORD()
    if not _ExitCode(process, ctypes.byref(code)) or code.value == 259:  # STILL_ACTIVE
        return None
    return code.value
