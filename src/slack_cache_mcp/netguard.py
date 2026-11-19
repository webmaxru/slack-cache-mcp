"""Runtime network audit hook: block non-loopback I/O in the core process.

Installed at the start of every CLI entry point. Loopback stays allowed
(asyncio on Windows uses a loopback socketpair).
"""

from __future__ import annotations

import ipaddress
import socket
import sys
from pathlib import Path
from typing import Any

_INSTALLED = False

# Exact executable basenames (no prefix matching).
# icacls removed: data-dir ACLs use Win32 APIs in paths.py (no subprocess).
_ALLOWED_EXACT = frozenset({"schtasks", "powershell", "pwsh"})


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
        return False


def _addr_is_loopback(address: Any) -> bool:
    if address is None:
        return False
    if isinstance(address, tuple) and address:
        return _is_loopback_host(address[0])
    if isinstance(address, str):
        return True
    return False


def _exe_basename(exe: str) -> str:
    base = exe.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return base.removesuffix(".exe")


def _argv_list(args: Any) -> list[str]:
    if not args:
        raise NetworkGuardError("subprocess.Popen with empty args blocked")
    if isinstance(args, (str, bytes)):
        raise NetworkGuardError("string/bytes subprocess commands blocked by netguard")
    try:
        return [str(a) for a in args]
    except Exception as exc:
        raise NetworkGuardError(f"subprocess.Popen blocked: {exc}") from exc


def _tokenize_windows_cmdline(cmdline: str) -> list[str]:
    """Minimal Windows command-line tokenizer for audit allowlisting."""
    out: list[str] = []
    buf: list[str] = []
    in_quote = False
    i = 0
    while i < len(cmdline):
        ch = cmdline[i]
        if ch == '"':
            in_quote = not in_quote
            i += 1
            continue
        if ch in {" ", "\t"} and not in_quote:
            if buf:
                out.append("".join(buf))
                buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if buf:
        out.append("".join(buf))
    return out


def _check_subprocess_windows_cmdline(executable: str | None, cmdline: str | bytes) -> None:
    if isinstance(cmdline, bytes):
        cmdline = cmdline.decode("utf-8", errors="replace")
    tokens = _tokenize_windows_cmdline(cmdline)
    if executable:
        # Prefer the resolved executable basename when present.
        tokens = [executable, *tokens[1:]] if tokens else [executable]
    _check_subprocess(tokens)


def _check_subprocess(args: Any) -> None:
    """Allow only specific read-only local helpers with validated argv."""
    if isinstance(args, (str, bytes)):
        raise NetworkGuardError("string/bytes subprocess commands blocked by netguard")
    argv = _argv_list(args)
    exe = _exe_basename(argv[0])
    if exe not in _ALLOWED_EXACT:
        raise NetworkGuardError(f"subprocess.Popen blocked for executable: {argv[0]!r}")

    rest_lower = [a.lower() for a in argv[1:]]

    if exe == "schtasks":
        # Read-only query only (task install uses XML generation + /Create later in M2
        # and will need an explicit allow once implemented).
        if rest_lower and rest_lower[0] == "/query":
            return
        raise NetworkGuardError("schtasks only /Query is allowed under netguard")

    if exe in {"powershell", "pwsh"}:
        # Require -NoProfile and only Get-AppxPackage (read-only Slack Store version lookup).
        if "-noprofile" not in rest_lower:
            raise NetworkGuardError("powershell requires -NoProfile")
        joined = " ".join(rest_lower)
        banned = (
            "invoke-webrequest",
            "invoke-restmethod",
            "invoke-expression",
            "iex ",
            "curl ",
            "wget ",
            "start-process",
            "new-object net.",
            "downloadstring",
            "downloadfile",
        )
        if any(b in joined for b in banned):
            raise NetworkGuardError("powershell network/exec cmdlets blocked")
        if "get-appxpackage" not in joined:
            raise NetworkGuardError("powershell only Get-AppxPackage is allowed")
        return

    raise NetworkGuardError(f"subprocess.Popen blocked for executable: {argv[0]!r}")


def _audit_hook(event: str, args: tuple[Any, ...]) -> None:
    if event == "socket.connect":
        if len(args) >= 2 and not _addr_is_loopback(args[1]):
            raise NetworkGuardError(f"socket.connect blocked to non-loopback: {args[1]!r}")
        return
    if event in {"socket.sendto", "socket.sendmsg"}:
        addr = args[-1] if args else None
        if isinstance(addr, tuple) and not _addr_is_loopback(addr):
            raise NetworkGuardError(f"{event} blocked to non-loopback: {addr!r}")
        return
    if event == "socket.getaddrinfo":
        host = args[0] if args else None
        if host is not None and not _is_loopback_host(host):
            raise NetworkGuardError(f"socket.getaddrinfo blocked for host: {host!r}")
        return
    if event in {
        "socket.gethostbyname",
        "socket.gethostbyname_ex",
        "socket.gethostbyaddr",
        "socket.gethostbyaddr_ex",
    }:
        host = args[0] if args else None
        if not _is_loopback_host(host):
            raise NetworkGuardError(f"{event} blocked for host: {host!r}")
        return
    if event == "socket.getnameinfo":
        if args and not _addr_is_loopback(args[0]):
            raise NetworkGuardError(f"socket.getnameinfo blocked for: {args[0]!r}")
        return
    if event in {"os.system", "os.startfile"}:
        raise NetworkGuardError(f"{event} blocked by netguard")
    if event in {"os.exec", "os.posix_spawn", "os.spawn"}:
        path = args[1] if event == "os.spawn" and len(args) > 1 else (args[0] if args else None)
        _check_subprocess([path] if path else None)
        return
    if event == "urllib.Request":
        raise NetworkGuardError("urllib.Request blocked by netguard")
    if event in {"http.client.connect", "http.client.send"}:
        raise NetworkGuardError(f"{event} blocked by netguard")
    if event == "webbrowser.open":
        raise NetworkGuardError("webbrowser.open blocked by netguard")
    if event == "subprocess.Popen":
        # Audit payload is (executable, args, cwd, env).
        # On Windows, `args` is list2cmdline'd to a string before the audit fires;
        # reconstruct a token list from that cmdline for allowlist checks.
        executable = args[0] if args else None
        argv = args[1] if len(args) > 1 else None
        if isinstance(argv, (list, tuple)):
            _check_subprocess(list(argv))
        elif isinstance(argv, (str, bytes)):
            _check_subprocess_windows_cmdline(executable, argv)
        elif executable is not None:
            _check_subprocess([executable])
        else:
            raise NetworkGuardError("subprocess.Popen with empty args blocked")
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


def allowlisted_exe_names() -> frozenset[str]:
    return _ALLOWED_EXACT


def path_basename(path: str | Path) -> str:
    return _exe_basename(str(path))
