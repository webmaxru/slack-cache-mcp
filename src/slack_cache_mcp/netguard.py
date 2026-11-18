"""Runtime network audit hook: block non-loopback I/O in the core process.

Installed at the start of every CLI entry point. Loopback stays allowed
(asyncio on Windows uses a loopback socketpair).
"""

from __future__ import annotations

import ipaddress
import socket
import sys
from typing import Any

_INSTALLED = False

# Basename prefixes allowed for subprocess.Popen (local admin helpers only).
_SUBPROCESS_ALLOW = (
    "schtasks",
    "powershell",
    "pwsh",
)


class NetworkGuardError(RuntimeError):
    """Raised when product code attempts forbidden network I/O."""


def _is_loopback_host(host: str | bytes | None) -> bool:
    if host is None:
        return False
    if isinstance(host, bytes):
        try:
            host = host.decode("utf-8", errors="replace")
        except Exception:
            return False
    host = host.strip().strip("[]")
    if host in {"localhost", "localhost."}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Unresolved names are not allowed through connect/sendto/getaddrinfo.
        return False


def _addr_is_loopback(address: Any) -> bool:
    if address is None:
        return False
    if isinstance(address, tuple) and address:
        return _is_loopback_host(address[0])
    if isinstance(address, str):
        # AF_UNIX / abstract — treat as local
        return True
    return False


def _check_subprocess(args: Any) -> None:
    if not args:
        raise NetworkGuardError("subprocess.Popen with empty args blocked")
    if isinstance(args, (str, bytes)):
        cmd = args if isinstance(args, str) else args.decode("utf-8", "replace")
        exe = cmd.split()[0] if cmd.strip() else ""
    else:
        try:
            exe = str(args[0])
        except Exception as exc:
            raise NetworkGuardError(f"subprocess.Popen blocked: {exc}") from exc
    base = exe.replace("\\", "/").rsplit("/", 1)[-1].lower()
    base = base.removesuffix(".exe")
    if not any(base == a or base.startswith(a) for a in _SUBPROCESS_ALLOW):
        raise NetworkGuardError(f"subprocess.Popen blocked for executable: {exe!r}")


def _audit_hook(event: str, args: tuple[Any, ...]) -> None:
    if event == "socket.connect":
        # args: (sock, address)
        if len(args) >= 2 and not _addr_is_loopback(args[1]):
            raise NetworkGuardError(f"socket.connect blocked to non-loopback: {args[1]!r}")
        return
    if event == "socket.sendto":
        # args vary; address often last
        addr = args[-1] if args else None
        if isinstance(addr, tuple) and not _addr_is_loopback(addr):
            raise NetworkGuardError(f"socket.sendto blocked to non-loopback: {addr!r}")
        return
    if event == "socket.getaddrinfo":
        host = args[0] if args else None
        if host is not None and not _is_loopback_host(host):
            raise NetworkGuardError(f"socket.getaddrinfo blocked for host: {host!r}")
        return
    if event == "urllib.Request":
        raise NetworkGuardError("urllib.Request blocked by netguard")
    if event in {"http.client.connect", "http.client.send"}:
        raise NetworkGuardError(f"{event} blocked by netguard")
    if event == "webbrowser.open":
        raise NetworkGuardError("webbrowser.open blocked by netguard")
    if event == "subprocess.Popen":
        # args: (executable, args, cwd, env) — CPython 3.8+ audit uses (args, ...)
        if args:
            _check_subprocess(args[0])
        return


def install() -> None:
    """Install the audit hook once. Safe to call multiple times."""
    global _INSTALLED
    if _INSTALLED:
        return
    sys.addaudithook(_audit_hook)
    _INSTALLED = True


def is_installed() -> bool:
    return _INSTALLED


def assert_loopback_allowed() -> None:
    """Smoke: connecting to 127.0.0.1 must succeed under the guard."""
    install()
    with socket.create_connection(("127.0.0.1", 9), timeout=0.2):
        pass
