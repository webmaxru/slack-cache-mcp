"""Allowlist copy of Slack profile files into a mirror (never writes to source)."""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from slack_cache_mcp.desktop.chromium.idb_values import idb_origin_dir_name

OpenHook = Callable[..., Any]


@dataclass
class SnapshotStats:
    copied: int = 0
    bytes: int = 0
    skipped_lock: int = 0
    skipped_unchanged: int = 0
    errors: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    files: dict[str, dict[str, Any]] = field(default_factory=dict)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_file(
    src: Path,
    dst: Path,
    *,
    tries: int = 6,
    open_func: OpenHook = open,
) -> str | None:
    """Copy one file with retries on sharing violations. Never opens src for write."""
    for i in range(tries):
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            # Explicit read-only open on source
            with open_func(src, "rb") as rf, open_func(dst, "wb") as wf:
                shutil.copyfileobj(rf, wf, length=1024 * 1024)
            return None
        except FileNotFoundError:
            return "vanished"
        except PermissionError:
            time.sleep(0.5 * (i + 1))
        except OSError:
            time.sleep(0.5 * (i + 1))
    return "failed"


def _size_stable(path: Path, *, pause_s: float = 0.25) -> bool:
    try:
        s1 = path.stat().st_size
        time.sleep(pause_s)
        s2 = path.stat().st_size
        return s1 == s2
    except OSError:
        return False


def _load_manifest(path: Path) -> dict[str, Any]:
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def _save_manifest(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _should_copy(src: Path, manifest_entry: dict[str, Any] | None) -> bool:
    try:
        st = src.stat()
    except OSError:
        return False
    if not manifest_entry:
        return True
    return not (
        manifest_entry.get("size") == st.st_size
        and manifest_entry.get("mtime_ns") == st.st_mtime_ns
    )


def snapshot_profile(
    source: Path,
    mirror: Path,
    *,
    open_func: OpenHook = open,
    require_stable_blobs: bool = True,
    include_http_cache: bool = False,
    http_cache_max_bytes: int = 50_000_000,
) -> SnapshotStats:
    """Copy allowlisted paths from source -> mirror. Never deletes source files."""
    source = Path(source).resolve()
    mirror = Path(mirror).resolve()
    if source == mirror or source in mirror.parents or mirror in source.parents:
        raise ValueError("Source and mirror directories must not overlap")
    mirror.mkdir(parents=True, exist_ok=True)
    stats = SnapshotStats()
    manifest_path = mirror / "manifest.json"
    manifest = _load_manifest(manifest_path)
    files_meta: dict[str, Any] = dict(manifest.get("files") or {})

    origin = idb_origin_dir_name()
    allow_trees = [
        source / "IndexedDB",
        # Local Storage may contain tokens; pipeline scrub removes the copy end-of-cycle
        # after allowlist parsing (PLAN §5.2).
        source / "Local Storage",
        source / "Session Storage",
    ]
    if include_http_cache:
        allow_trees.append(source / "Cache")
    allow_files = [
        source / "storage" / "root-state.json",
        source / "local-settings.json",
        # M1 fixture side-car for localConfig (not present on real Slack)
        source / "Local Storage" / "localConfig_v2.json",
        # M3 synthetic side-cars
        source / "Session Storage" / "session_entries.json",
        source / "Local Storage" / "local_entries.json",
        source / "Cache" / "cache_entries.json",
        source / "Notifications" / "notifications_win.json",
        source / "notifications_win.json",
    ]

    def handle_file(src_file: Path, rel: Path, *, is_blob: bool) -> None:
        nonlocal stats
        if src_file.name == "LOCK":
            stats.skipped_lock += 1
            return
        rel_s = rel.as_posix()
        prev = files_meta.get(rel_s)
        if not _should_copy(src_file, prev):
            stats.skipped_unchanged += 1
            return
        if is_blob and require_stable_blobs and not _size_stable(src_file):
            stats.errors.append(f"{rel_s}: size not stable")
            return
        dst = mirror / rel
        try:
            before = src_file.stat()
        except OSError as exc:
            stats.errors.append(f"{rel_s}: pre-stat {exc}")
            return
        err = copy_file(src_file, dst, open_func=open_func)
        if err is None:
            try:
                st = src_file.stat()
                if (st.st_size, st.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                    # Changed while copying: don't record it so the next cycle re-copies.
                    stats.errors.append(f"{rel_s}: changed during copy")
                    return
                digest = _sha256_file(dst)
                files_meta[rel_s] = {
                    "size": st.st_size,
                    "mtime_ns": st.st_mtime_ns,
                    "sha256": digest,
                }
                stats.copied += 1
                stats.bytes += st.st_size
            except OSError as exc:
                stats.errors.append(f"{rel_s}: post-stat {exc}")
        else:
            stats.errors.append(f"{rel_s}: {err}")

    cache_bytes = 0
    for tree in allow_trees:
        if not tree.exists():
            stats.missing.append(tree.name)
            continue
        for p in tree.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(source)
            is_blob = ".indexeddb.blob" in p.as_posix().replace("\\", "/")
            # Cap HTTP cache copy size (PLAN §4 #8)
            if include_http_cache and "Cache" in rel.parts:
                try:
                    sz = p.stat().st_size
                except OSError:
                    continue
                if cache_bytes + sz > http_cache_max_bytes:
                    stats.errors.append(f"{rel.as_posix()}: http cache size cap")
                    continue
                cache_bytes += sz
            handle_file(p, rel, is_blob=is_blob)

    for f in allow_files:
        if not f.exists():
            stats.missing.append(f.as_posix())
            continue
        handle_file(f, f.relative_to(source), is_blob=False)

    # Note: files that disappear from source are NOT removed from mirror.
    stats.files = files_meta
    _save_manifest(
        manifest_path,
        {
            "source": str(source),
            "origin": origin,
            "files": files_meta,
            "updated_at": time.time(),
        },
    )
    return stats


def assert_source_not_written(source: Path, open_calls: list[tuple[Any, ...]]) -> None:
    """Test helper: ensure no open(..., 'w'/'a'/'x'/'r+') under source."""
    src = str(source.resolve())
    for args in open_calls:
        if not args:
            continue
        path = str(args[0])
        mode = args[1] if len(args) > 1 else "r"
        if path.startswith(src) or Path(path).resolve().as_posix().startswith(
            Path(src).as_posix()
        ):
            if isinstance(mode, str) and any(c in mode for c in "wax+"):
                # read modes with '+' are read/write
                if mode == "rb" or mode == "r" or mode.startswith("r") and "+" not in mode:
                    continue
                if "r" in mode and "+" not in mode and "w" not in mode and "a" not in mode:
                    continue
                raise AssertionError(f"write-mode open on source: {args!r}")
