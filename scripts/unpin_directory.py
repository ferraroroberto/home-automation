"""Free a directory pinned by a process wedged inside termination (#681).

Windows will not delete a directory that is some live process's current
working directory. Normally that resolves itself — the process exits, the cwd
handle closes, the directory frees. A process **wedged inside termination**
never gets there: `ExitStatus` is set, so `taskkill` answers "There is no
running instance of the task" and `GetExitCodeProcess` reports a clean `0`, but
a thread never finished dying, so the address space and the cwd handle are
still held. The directory stays pinned with no process anyone can kill.

`tests/e2e/conftest.py`'s `_neutral_driver_cwd` stops this repo's e2e runs from
creating such pins in the first place. This script is the remedy for the ones
already on disk, and for a pin created by anything else.

The remedy is to close the offending handle out from under the wedged process:
read `RTL_USER_PROCESS_PARAMETERS.CurrentDirectory.Handle` from its PEB and
close it remotely with `NtDuplicateObject(..., DUPLICATE_CLOSE_SOURCE)`.
Verified 2026-08-22 against six pinned worktrees on the fleet host — every
close returned `STATUS_SUCCESS`, every directory deleted immediately, no
reboot. That supersedes home-automation#581, whose only prescribed remedy was
rebooting the host.

**A live holder is never touched.** Yanking the cwd handle out of a healthy
process is the kind of thing that corrupts a running browser or a running
build, so `--close` acts only on processes proven wedged: Win32 reports them
exited *and* `GetProcessTimes` reports no exit time. A live holder is reported
and left alone — the same rule as the fleet's shared-Chrome-profile and
safe-restart conventions. A process this cannot inspect is `UNKNOWN`, never
folded into "clear".

Deliberately self-contained (stdlib + ctypes, no repo imports). The PEB walk
overlaps `tests/e2e/_browser_sweep.py`, but that module is vendored
byte-verbatim from project-scaffolding and cannot grow this repo's additions,
and an operator script under `scripts/` must not depend on the test tree.

Usage::

    .venv\\Scripts\\python.exe scripts\\unpin_directory.py <path>            # report only
    .venv\\Scripts\\python.exe scripts\\unpin_directory.py <path> --close    # free it

Exit codes: 0 = nothing pinning it (or `--close` freed it), 1 = still pinned,
2 = could not establish the facts.
"""

from __future__ import annotations

import argparse
import ctypes
import sys
from pathlib import Path
from typing import Iterator, NamedTuple, Optional

if sys.platform != "win32":
    raise ImportError("unpin_directory is Windows-only")

from ctypes import wintypes

# Piped/redirected stdout falls back to cp1252, where the status glyphs below
# raise UnicodeEncodeError and exit 1 — which would be indistinguishable from
# this script's documented "1 = still pinned". Same guard every sibling script
# in this directory carries.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:  # pragma: no cover - non-reconfigurable stream
    pass

STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_DUP_HANDLE = 0x0040
TH32CS_SNAPPROCESS = 0x0002
DUPLICATE_CLOSE_SOURCE = 0x0001

#: x64 offsets: PEB.ProcessParameters, then within
#: RTL_USER_PROCESS_PARAMETERS the CURDIR struct — a UNICODE_STRING DosPath
#: (Length, MaximumLength, then a Buffer pointer 8 bytes in) followed by the
#: directory HANDLE the process actually holds.
PEB_PROCESS_PARAMETERS_OFFSET = 0x20
CURDIR_DOSPATH_OFFSET = 0x38
CURDIR_DOSPATH_BUFFER_OFFSET = 0x40
CURDIR_HANDLE_OFFSET = 0x48

STATE_WEDGED = "wedged"
STATE_LIVE = "live"
STATE_UNKNOWN = "unknown"

DELETE = 0x00010000
FILE_SHARE_ALL = 0x00000007
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
INVALID_HANDLE_VALUE = 0xFFFFFFFFFFFFFFFF
ERROR_SHARING_VIOLATION = 32


class Holder(NamedTuple):
    """One process holding *scope* as its working directory."""

    pid: int
    name: str
    cwd: str
    state: str
    """``wedged`` (safe to unpin), ``live`` (hands off), ``unknown`` (unprovable)."""
    handle_value: int


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = (
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    )


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_ntdll = ctypes.WinDLL("ntdll")
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]


def _open(pid: int, access: int) -> int:
    handle = _k32.OpenProcess(access, False, pid)
    return int(handle) if handle else 0


def _read(handle: int, address: int, size: int) -> Optional[bytes]:
    buffer = ctypes.create_string_buffer(size)
    read = ctypes.c_size_t()
    ok = _k32.ReadProcessMemory(
        wintypes.HANDLE(handle), ctypes.c_void_p(address), buffer,
        ctypes.c_size_t(size), ctypes.byref(read),
    )
    return buffer.raw if ok and read.value == size else None


def _process_state(pid: int) -> str:
    """``live`` / ``wedged`` / ``unknown`` — never a guess.

    Win32 calls a process "exited" the moment `ExitStatus` is set, which for a
    process stuck in termination is a lie of omission: `GetProcessTimes` still
    reports no exit time. That disagreement is the whole signature.
    """
    handle = _open(pid, PROCESS_QUERY_LIMITED_INFORMATION)
    if not handle:
        return STATE_UNKNOWN
    try:
        code = wintypes.DWORD()
        if not _k32.GetExitCodeProcess(wintypes.HANDLE(handle), ctypes.byref(code)):
            return STATE_UNKNOWN
        if int(code.value) == STILL_ACTIVE:
            return STATE_LIVE
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        ok = _k32.GetProcessTimes(
            wintypes.HANDLE(handle), ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel), ctypes.byref(user),
        )
        if not ok:
            return STATE_UNKNOWN
        exit_stamp = (int(exited.dwHighDateTime) << 32) | int(exited.dwLowDateTime)
        return STATE_WEDGED if exit_stamp == 0 else STATE_LIVE
    finally:
        _k32.CloseHandle(wintypes.HANDLE(handle))


def _iter_processes() -> Iterator[tuple[int, str]]:
    snapshot = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or int(snapshot) == 0xFFFFFFFFFFFFFFFF:
        return
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not _k32.Process32FirstW(wintypes.HANDLE(int(snapshot)), ctypes.byref(entry)):
            return
        while True:
            yield int(entry.th32ProcessID), str(entry.szExeFile)
            if not _k32.Process32NextW(wintypes.HANDLE(int(snapshot)), ctypes.byref(entry)):
                return
    finally:
        _k32.CloseHandle(wintypes.HANDLE(int(snapshot)))


def _read_curdir(handle: int) -> Optional[tuple[str, int]]:
    """``(cwd, handle_value)`` from the process's PEB, or None on any failure."""
    basic = (ctypes.c_size_t * 6)()
    returned = ctypes.c_ulong()
    status = _ntdll.NtQueryInformationProcess(
        wintypes.HANDLE(handle), 0, ctypes.byref(basic), ctypes.sizeof(basic),
        ctypes.byref(returned),
    )
    if status != 0 or not basic[1]:
        return None
    raw = _read(handle, int(basic[1]) + PEB_PROCESS_PARAMETERS_OFFSET, 8)
    if not raw:
        return None
    params = int.from_bytes(raw, "little")
    if not params:
        return None
    raw = _read(handle, params + CURDIR_DOSPATH_OFFSET, 2)
    if not raw:
        return None
    length = int.from_bytes(raw, "little")
    raw = _read(handle, params + CURDIR_DOSPATH_BUFFER_OFFSET, 8)
    if not raw or not length:
        return None
    buffer_address = int.from_bytes(raw, "little")
    if not buffer_address:
        return None
    text = _read(handle, buffer_address, length)
    if text is None:
        return None
    raw = _read(handle, params + CURDIR_HANDLE_OFFSET, 8)
    if raw is None:
        return None
    cwd = text.decode("utf-16-le", errors="replace").rstrip("\\")
    return (cwd, int.from_bytes(raw, "little")) if cwd else None


def find_holders(scope: Path) -> list[Holder]:
    """Every process whose working directory is *scope* or lives under it."""
    scope_resolved = scope.resolve()
    holders: list[Holder] = []
    for pid, name in _iter_processes():
        # Read-only rights: reporting must never ask for the right to close a
        # handle. `unpin` requests PROCESS_DUP_HANDLE separately, when it acts.
        handle = _open(pid, PROCESS_QUERY_INFORMATION | PROCESS_VM_READ)
        if not handle:
            continue
        try:
            curdir = _read_curdir(handle)
        finally:
            _k32.CloseHandle(wintypes.HANDLE(handle))
        if curdir is None:
            continue
        cwd, handle_value = curdir
        try:
            resolved = Path(cwd).resolve()
        except (OSError, ValueError):
            continue
        if resolved != scope_resolved and scope_resolved not in resolved.parents:
            continue
        holders.append(Holder(pid, name, cwd, _process_state(pid), handle_value))
    return holders


def is_delete_ready(scope: Path) -> Optional[bool]:
    """Can *scope* be deleted right now? ``None`` when that can't be established.

    This — not the holder list — is the authoritative answer, and the two are
    not interchangeable. A cwd handle is opened without `FILE_SHARE_DELETE`, so
    asking for `DELETE` access is exactly the question Windows answers with a
    sharing violation while something pins the directory. The holder list is a
    *diagnosis* (who to blame), and it is read from each process's PEB, where
    the cwd **string survives its handle being closed** — so re-reading it after
    a close would report a pin that is already gone.
    """
    ctypes.set_last_error(0)
    handle = _k32.CreateFileW(
        str(scope), DELETE, FILE_SHARE_ALL, None, OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS, None,
    )
    value = int(handle) if handle else 0
    if value and value != INVALID_HANDLE_VALUE:
        _k32.CloseHandle(wintypes.HANDLE(value))
        return True
    if ctypes.get_last_error() == ERROR_SHARING_VIOLATION:
        return False
    return None


def unpin(holder: Holder) -> bool:
    """Close *holder*'s cwd handle remotely. Only ever called on a wedged one."""
    handle = _open(holder.pid, PROCESS_DUP_HANDLE)
    if not handle:
        return False
    try:
        status = _ntdll.NtDuplicateObject(
            wintypes.HANDLE(handle), wintypes.HANDLE(holder.handle_value),
            None, None, 0, 0, DUPLICATE_CLOSE_SOURCE,
        )
        return status == 0
    finally:
        _k32.CloseHandle(wintypes.HANDLE(handle))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", help="the pinned directory")
    parser.add_argument(
        "--close", action="store_true",
        help="close the wedged holders' cwd handles (default: report only)",
    )
    args = parser.parse_args(argv)

    scope = Path(args.path)
    if not scope.exists():
        print(f"✅ {scope} does not exist — nothing to unpin")
        return 0

    ready = is_delete_ready(scope)
    if ready is None:
        print(f"❓ could not establish whether {scope} is pinned — reporting unknown")
        return 2

    holders = find_holders(scope)
    for holder in holders:
        icon = {STATE_WEDGED: "⚠️", STATE_LIVE: "🔴", STATE_UNKNOWN: "❓"}[holder.state]
        print(f"{icon} {holder.name}#{holder.pid} [{holder.state}] cwd={holder.cwd}")

    if ready:
        print(f"✅ {scope} is not pinned — it can be deleted")
        return 0

    live = [h for h in holders if h.state == STATE_LIVE]
    unknown = [h for h in holders if h.state == STATE_UNKNOWN]
    wedged = [h for h in holders if h.state == STATE_WEDGED]

    if live:
        print(
            f"🔴 {len(live)} live holder(s) — refusing to touch them. A live process's "
            f"cwd handle is legitimately in use; stop the process itself, then re-run."
        )
        return 1
    if not wedged:
        print(
            f"❓ {scope} is pinned, but no wedged holder explains it — the holder does "
            f"not name it as a working directory, or could not be inspected "
            f"({len(unknown)} unreadable). Nothing was touched."
        )
        return 2
    if not args.close:
        print(f"ℹ️ {len(wedged)} wedged holder(s); re-run with --close to free {scope}")
        return 1

    freed = sum(1 for holder in wedged if unpin(holder))
    print(f"🧹 closed {freed}/{len(wedged)} wedged cwd handle(s)")
    # Re-probe the filesystem, never the holder list: a closed handle leaves the
    # PEB's cwd string behind, so re-reading holders would report a false pin.
    after = is_delete_ready(scope)
    if after is None:
        print(f"❓ could not confirm {scope} is free — reporting unknown, not success")
        return 2
    if not after:
        print(f"❌ {scope} is still pinned")
        return 1
    print(f"✅ {scope} is free — it can now be deleted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
