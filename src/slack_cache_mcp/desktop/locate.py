"""Discover Slack desktop profile directories (Windows Store/direct, macOS, Linux)."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from slack_cache_mcp.config import local_appdata, roaming_appdata
from slack_cache_mcp.desktop.chromium.idb_values import idb_origin_dir_name


@dataclass(frozen=True)
class SlackProfile:
    path: Path
    kind: str
    valid: bool
    primary: bool = False
    idb_mtime_ns: int = 0


def _is_valid_profile(path: Path) -> bool:
    origin = idb_origin_dir_name()
    idb_current = path / "IndexedDB" / f"{origin}.leveldb" / "CURRENT"
    root_state = path / "storage" / "root-state.json"
    return idb_current.is_file() or root_state.is_file()


def _idb_mtime(path: Path) -> int:
    origin = idb_origin_dir_name()
    idb = path / "IndexedDB" / f"{origin}.leveldb"
    newest = 0
    if idb.is_dir():
        for p in idb.rglob("*"):
            if p.is_file():
                try:
                    newest = max(newest, p.stat().st_mtime_ns)
                except OSError:
                    continue
    return newest


def windows_candidates(env: dict[str, str] | None = None) -> list[tuple[Path, str]]:
    e = env if env is not None else dict(os.environ)
    out: list[tuple[Path, str]] = []
    local = Path(e["LOCALAPPDATA"]) if e.get("LOCALAPPDATA") else local_appdata()
    roaming = Path(e["APPDATA"]) if e.get("APPDATA") else roaming_appdata()

    store_fixed = (
        local
        / "Packages"
        / "91750D7E.Slack_8she8kybcnzg4"
        / "LocalCache"
        / "Roaming"
        / "Slack"
    )
    out.append((store_fixed, "win-store"))

    packages = local / "Packages"
    if packages.is_dir():
        for pkg in packages.glob("*Slack*"):
            cand = pkg / "LocalCache" / "Roaming" / "Slack"
            if cand != store_fixed:
                out.append((cand, "win-store"))

    out.append((roaming / "Slack", "win-direct"))
    return out


def macos_candidates(home: Path | None = None) -> list[tuple[Path, str]]:
    h = home or Path.home()
    return [
        (h / "Library" / "Application Support" / "Slack", "mac-direct"),
        (
            h
            / "Library"
            / "Containers"
            / "com.tinyspeck.slackmacgap"
            / "Data"
            / "Library"
            / "Application Support"
            / "Slack",
            "mac-appstore",
        ),
    ]


def linux_candidates(home: Path | None = None, env: dict[str, str] | None = None) -> list[tuple[Path, str]]:
    h = home or Path.home()
    e = env if env is not None else dict(os.environ)
    out: list[tuple[Path, str]] = []
    xdg = e.get("XDG_CONFIG_HOME")
    if xdg:
        out.append((Path(xdg) / "Slack", "linux"))
    out.append((h / ".config" / "Slack", "linux"))
    out.append((h / ".var" / "app" / "com.slack.Slack" / "config" / "Slack", "linux-flatpak"))
    out.append((h / "snap" / "slack" / "current" / ".config" / "Slack", "linux-snap"))
    return out


def discover_profiles(
    *,
    overrides: list[Path] | None = None,
    env: dict[str, str] | None = None,
    home: Path | None = None,
    platform: str | None = None,
) -> list[SlackProfile]:
    plat = platform or sys.platform
    candidates: list[tuple[Path, str]] = []
    if overrides:
        for p in overrides:
            candidates.append((Path(p), "override"))
    elif plat == "win32":
        candidates.extend(windows_candidates(env=env))
    elif plat == "darwin":
        candidates.extend(macos_candidates(home=home))
    else:
        candidates.extend(linux_candidates(home=home, env=env))

    seen: set[Path] = set()
    found: list[SlackProfile] = []
    for path, kind in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        valid = _is_valid_profile(path)
        if not valid and kind != "override":
            # Still record overrides even if incomplete; skip missing non-override
            if not path.exists():
                continue
        if not path.exists() and kind != "override":
            continue
        found.append(
            SlackProfile(
                path=path,
                kind=kind,
                valid=_is_valid_profile(path),
                idb_mtime_ns=_idb_mtime(path) if path.exists() else 0,
            )
        )

    if not found:
        return []

    # Mark primary = newest IndexedDB mtime among valid
    valid_ones = [p for p in found if p.valid]
    primary_path = None
    if valid_ones:
        primary_path = max(valid_ones, key=lambda p: p.idb_mtime_ns).path

    return [
        SlackProfile(
            path=p.path,
            kind=p.kind,
            valid=p.valid,
            primary=(primary_path is not None and p.path == primary_path),
            idb_mtime_ns=p.idb_mtime_ns,
        )
        for p in found
    ]


def redact_path(path: Path) -> str:
    s = str(path)
    for var, getter in (
        ("LOCALAPPDATA", local_appdata),
        ("APPDATA", roaming_appdata),
    ):
        try:
            root = str(getter())
            if root and s.startswith(root):
                return s.replace(root, f"%{var}%", 1)
        except Exception:
            continue
    home = str(Path.home())
    if s.startswith(home):
        return "~" + s[len(home) :]
    return s
