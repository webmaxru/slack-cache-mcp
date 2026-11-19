"""Data-directory hardening and path-disjointness checks."""

from __future__ import annotations

import contextlib
import os
import stat
import sys
from pathlib import Path


class PathOverlapError(ValueError):
    """Raised when the archive data dir overlaps a Slack source profile."""


def paths_overlap(a: Path, b: Path) -> bool:
    try:
        ar = a.resolve()
        br = b.resolve()
    except OSError:
        ar, br = a, b
    if ar == br:
        return True
    try:
        ar.relative_to(br)
        return True
    except ValueError:
        pass
    try:
        br.relative_to(ar)
        return True
    except ValueError:
        pass
    return False


def assert_data_dir_outside_sources(data_dir: Path, sources: list[Path]) -> None:
    for src in sources:
        if paths_overlap(data_dir, src):
            raise PathOverlapError(
                f"data_dir {data_dir} overlaps Slack source {src}; "
                "choose a directory outside the profile"
            )


def secure_data_dir(path: Path) -> None:
    """Create `path` if needed and restrict to the current user where possible."""
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        _secure_windows(path)
    else:
        with contextlib.suppress(OSError):
            os.chmod(path, stat.S_IRWXU)  # 0o700


def _secure_windows(path: Path) -> None:
    """Best-effort owner-only ACL via Win32 APIs (no subprocess / netguard)."""
    if sys.platform != "win32":
        return
    with contextlib.suppress(Exception):
        import ctypes
        from ctypes import wintypes

        # getattr keeps mypy happy on non-Windows typecheck hosts (no ctypes.windll).
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return
        advapi32 = windll.advapi32
        kernel32 = windll.kernel32

        # Convert SDDL: D:P(A;OICI;FA;;;OW) — protect, full access to owner only
        sddl = "D:P(A;OICI;FA;;;OW)"
        p_sd = ctypes.c_void_p()
        p_sz = wintypes.ULONG()
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(p_sd), ctypes.byref(p_sz)
        ):
            return
        try:
            # SECURITY_INFORMATION: DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION
            DACL_SECURITY_INFORMATION = 0x00000004
            PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
            advapi32.SetFileSecurityW(
                str(path),
                DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                p_sd,
            )
        finally:
            kernel32.LocalFree(p_sd)
