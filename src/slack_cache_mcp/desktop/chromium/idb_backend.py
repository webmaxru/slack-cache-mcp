"""IndexedDB backend protocol + detectors (LevelDB now, SQLite when present)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from slack_cache_mcp.desktop.chromium.idb_values import idb_origin_dir_name


@dataclass(frozen=True)
class IdbRecord:
    """One IndexedDB value sighting (live or historical/deleted)."""

    database: str
    store: str
    key: str
    value: Any | None
    seq: int | None
    state: str  # live | deleted | unknown
    source: str  # idb.leveldb | idb.leveldb.historical | idb.sqlite | idb.sidecar
    error: str | None = None
    blob_ref: str | None = None


class IdbBackend(Protocol):
    def list_databases(self) -> list[str]: ...

    def iter_records(
        self,
        database: str | None = None,
        store: str | None = None,
        *,
        include_deleted: bool = True,
    ) -> Iterator[IdbRecord]: ...

    def read_blob(self, ref: int | str) -> bytes | None: ...


def detect_idb_backend(
    profile_or_mirror: Path, *, slack_origin_only: bool = False
) -> str:
    """Return 'sqlite' | 'leveldb' | 'missing' | 'unknown' without opening live DBs unsafely.

    Detection opens files read-only for a 16-byte header check only.
    When ``slack_origin_only`` (Edge doctor), only inspect the Slack app origin IDB dirs.
    """
    origin = idb_origin_dir_name()
    idb = Path(profile_or_mirror) / "IndexedDB"
    if not idb.is_dir():
        return "missing"
    if slack_origin_only:
        scan_roots = [idb / f"{origin}.leveldb", idb / f"{origin}.blob"]
        file_iter: list[Path] = []
        for root in scan_roots:
            if root.is_dir():
                file_iter.extend(p for p in root.rglob("*") if p.is_file())
    else:
        file_iter = list(idb.rglob("*"))
    for p in file_iter:
        if not p.is_file() or p.name == "LOCK":
            continue
        try:
            with open(p, "rb") as fh:
                head = fh.read(16)
        except OSError:
            continue
        if head.startswith(b"SQLite format 3"):
            return "sqlite"
    if (idb / f"{origin}.leveldb").is_dir():
        return "leveldb"
    if not slack_origin_only and (
        any(idb.glob("*.indexeddb.leveldb")) or any(idb.glob("*.indexeddb.blob"))
    ):
        return "leveldb"
    return "unknown"


def open_idb_backend(profile_or_mirror: Path) -> IdbBackend:
    """Pick a backend for a *mirror copy* (never the live Slack profile)."""
    kind = detect_idb_backend(profile_or_mirror)
    if kind == "sqlite":
        from slack_cache_mcp.desktop.chromium.idb_sqlite import SqliteIdbBackend

        return SqliteIdbBackend(Path(profile_or_mirror))
    from slack_cache_mcp.desktop.chromium.idb_leveldb import LevelDbIdbBackend

    return LevelDbIdbBackend(Path(profile_or_mirror))
