"""IdbBackend for Chromium IndexedDB SQLite backing store (copy-then-open)."""

from __future__ import annotations

import re
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from slack_cache_mcp.desktop.chromium.idb_backend import IdbRecord
from slack_cache_mcp.desktop.chromium.idb_values import unwrap
from slack_cache_mcp.desktop.sanitize import sanitize

# Cap heuristic harvest so a large IndexedDB SQLite store cannot OOM the daemon.
_MAX_ROWS_PER_TABLE = 5_000

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote_ident(name: str) -> str:
    """Quote a SQLite identifier; reject NUL / empty names."""
    if not name or "\x00" in name:
        raise ValueError("invalid SQL identifier")
    # Always quote; escape embedded double quotes per SQLite rules.
    return '"' + name.replace('"', '""') + '"'


def _safe_column(name: str, allowed: set[str]) -> str | None:
    """Return quoted column only if it appears in PRAGMA table_info output."""
    if name not in allowed:
        return None
    if not _IDENT_RE.match(name) and '"' in name:
        # Still quote non-plain names that PRAGMA reported.
        pass
    return _quote_ident(name)


def _copy_sqlite_bundle(src_db: Path, dest_dir: Path) -> Path:
    """Copy db + wal/shm/journal siblings into dest_dir; return dest db path."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src_db.name
    shutil.copy2(src_db, dest)
    for suffix in ("-wal", "-shm", "-journal"):
        sib = Path(str(src_db) + suffix)
        if sib.is_file():
            shutil.copy2(sib, dest_dir / sib.name)
    return dest


def _open_copy_readonly(db_path: Path) -> sqlite3.Connection:
    """Replay WAL on the copy (RW once), then open immutable RO."""
    # Opening RW on the copy applies WAL; never touch the live file.
    rw = sqlite3.connect(str(db_path))
    try:
        rw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass
    finally:
        rw.close()
    uri = f"file:{db_path.as_posix()}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


class SqliteIdbBackend:
    """Detect and read SQLite-backed IndexedDB from a mirror copy."""

    def __init__(self, mirror: Path) -> None:
        self.mirror = Path(mirror)
        self._dbs = self._find_sqlite_files()

    def _find_sqlite_files(self) -> list[Path]:
        idb = self.mirror / "IndexedDB"
        found: list[Path] = []
        if not idb.is_dir():
            return found
        for p in idb.rglob("*"):
            if not p.is_file() or p.name == "LOCK":
                continue
            name = p.name
            if name.endswith(("-wal", "-shm", "-journal")):
                continue
            try:
                with open(p, "rb") as fh:
                    head = fh.read(16)
            except OSError:
                continue
            if head.startswith(b"SQLite format 3"):
                found.append(p)
        return found

    def list_databases(self) -> list[str]:
        return [p.stem for p in self._dbs]

    def read_blob(self, ref: int | str) -> bytes | None:
        return None

    def iter_records(
        self,
        database: str | None = None,
        store: str | None = None,
        *,
        include_deleted: bool = True,
    ) -> Iterator[IdbRecord]:
        del include_deleted  # SQLite IDB deleted rows not always retained
        for src in self._dbs:
            db_name = src.stem
            if database is not None and database not in (db_name, src.name):
                continue
            with tempfile.TemporaryDirectory(prefix="slackcache-idb-sqlite-") as tmp:
                dest = _copy_sqlite_bundle(src, Path(tmp))
                try:
                    conn = _open_copy_readonly(dest)
                except sqlite3.Error as exc:
                    yield IdbRecord(
                        database=db_name,
                        store=store or "unknown",
                        key="",
                        value=None,
                        seq=None,
                        state="unknown",
                        source="idb.sqlite",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    continue
                try:
                    yield from self._scan_conn(conn, db_name=db_name, store=store)
                finally:
                    conn.close()

    def _scan_conn(
        self,
        conn: sqlite3.Connection,
        *,
        db_name: str,
        store: str | None,
    ) -> Iterator[IdbRecord]:
        # Chromium SQLite IDB schema varies; pull BLOB columns heuristically.
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        for table in tables:
            if not isinstance(table, str) or not table:
                continue
            if store is not None and store != table:
                continue
            try:
                qtable = _quote_ident(table)
                cols = [
                    str(r[1])
                    for r in conn.execute(f"PRAGMA table_info({qtable})").fetchall()
                    if r[1] is not None
                ]
            except (sqlite3.Error, ValueError):
                continue
            allowed = set(cols)
            blob_cols = [c for c in cols if c.lower() in {"value", "value_data", "data", "blob"}]
            key_cols = [c for c in cols if c.lower() in {"key", "key_data", "object_store_id"}]
            if not blob_cols:
                continue
            key_col = key_cols[0] if key_cols else None
            blob_col = blob_cols[0]
            q_blob = _safe_column(blob_col, allowed)
            if q_blob is None:
                continue
            select_cols = [q_blob]
            key_idx: int | None = None
            if key_col is not None:
                q_key = _safe_column(key_col, allowed)
                if q_key is not None:
                    select_cols.insert(0, q_key)
                    key_idx = 0
            blob_idx = len(select_cols) - 1
            sql = (
                f"SELECT {', '.join(select_cols)} FROM {qtable} LIMIT {_MAX_ROWS_PER_TABLE}"
            )
            try:
                cur = conn.execute(sql)
            except sqlite3.Error:
                continue
            for row in cur:
                raw_val = row[blob_idx]
                key_s = str(row[key_idx]) if key_idx is not None else ""
                decoded: Any | None = None
                err: str | None = None
                if isinstance(raw_val, (bytes, memoryview)):
                    try:
                        decoded = unwrap(bytes(raw_val))
                        decoded = sanitize(decoded)
                    except Exception as exc:  # noqa: BLE001
                        err = f"{type(exc).__name__}: {exc}"
                yield IdbRecord(
                    database=db_name,
                    store=table,
                    key=key_s,
                    value=decoded,
                    seq=None,
                    state="live",
                    source="idb.sqlite",
                    error=err,
                )
