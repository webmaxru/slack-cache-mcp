"""Named mutex / lockfile so only one daemon runs per user session."""

from __future__ import annotations

import atexit
import contextlib
import sys
from pathlib import Path
from typing import Any

_MUTEX_NAME = "Local\\slackcache-daemon"
_HANDLE: Any = None
_LOCK_FILE: Path | None = None


class AlreadyRunningError(RuntimeError):
    """Another slackcache daemon holds the single-instance lock."""


def acquire(*, lock_dir: Path | None = None) -> None:
    """Acquire the single-instance lock or raise AlreadyRunningError."""
    global _HANDLE, _LOCK_FILE
    if sys.platform == "win32":
        _acquire_windows()
        return
    _acquire_lockfile(lock_dir or Path.cwd())


def release() -> None:
    global _HANDLE, _LOCK_FILE
    if sys.platform == "win32" and _HANDLE is not None:
        try:
            import ctypes

            windll = getattr(ctypes, "windll", None)
            if windll is not None:
                windll.kernel32.CloseHandle(_HANDLE)
        except Exception:
            pass
        _HANDLE = None
    if _LOCK_FILE is not None:
        with contextlib.suppress(OSError):
            _LOCK_FILE.unlink(missing_ok=True)
        _LOCK_FILE = None


def _acquire_windows() -> None:
    global _HANDLE
    import ctypes

    windll = getattr(ctypes, "windll", None)
    if windll is None:
        raise AlreadyRunningError("windll unavailable")
    kernel32 = windll.kernel32
    # CreateMutexW(lpMutexAttributes, bInitialOwner, lpName)
    handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    if not handle:
        raise AlreadyRunningError("CreateMutex failed")
    err = kernel32.GetLastError()
    # ERROR_ALREADY_EXISTS = 183
    if err == 183:
        kernel32.CloseHandle(handle)
        raise AlreadyRunningError("daemon already running")
    _HANDLE = handle
    atexit.register(release)


def _acquire_lockfile(lock_dir: Path) -> None:
    global _LOCK_FILE
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / "daemon.lock"
    if path.is_file():
        # Stale check: if pid not alive, take over (best-effort)
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
            import os

            os.kill(pid, 0)
            raise AlreadyRunningError(f"daemon already running (pid={pid})")
        except (ValueError, OSError, ProcessLookupError):
            path.unlink(missing_ok=True)
    import os

    path.write_text(str(os.getpid()), encoding="utf-8")
    _LOCK_FILE = path
    atexit.register(release)
