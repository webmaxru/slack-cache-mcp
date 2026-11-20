"""IdbBackend over LevelDB + blob directory (plus synthetic side-car fixtures)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from slack_cache_mcp.desktop.chromium.idb_backend import IdbRecord
from slack_cache_mcp.desktop.chromium.idb_values import idb_origin_dir_name
from slack_cache_mcp.desktop.chromium.leveldb_records import harvest_leveldb_dir
from slack_cache_mcp.desktop.sanitize import sanitize


def _blob_dir(root: Path) -> Path:
    return root / "IndexedDB" / f"{idb_origin_dir_name()}.blob"


def _leveldb_dir(root: Path) -> Path:
    return root / "IndexedDB" / f"{idb_origin_dir_name()}.leveldb"


def _make_blob_lookup(blob_root: Path) -> Any:
    cache: dict[int, bytes] = {}

    def lookup(index: int) -> bytes | None:
        if index in cache:
            return cache[index]
        # Chromium blob layout: <db>/<xx>/<n> — try common paths and rglob by leaf name
        candidates = list(blob_root.rglob(str(index)))
        for c in candidates:
            if c.is_file():
                try:
                    data = c.read_bytes()
                except OSError:
                    continue
                cache[index] = data
                return data
        return None

    return lookup


class LevelDbIdbBackend:
    """Read IndexedDB from a mirror: LevelDB harvest + optional JSON side-cars."""

    def __init__(self, mirror: Path) -> None:
        self.mirror = Path(mirror)
        self._blob_root = _blob_dir(self.mirror)
        self._leveldb = _leveldb_dir(self.mirror)
        self._blob_lookup = _make_blob_lookup(self._blob_root) if self._blob_root.is_dir() else None

    def list_databases(self) -> list[str]:
        names: list[str] = []
        idb = self.mirror / "IndexedDB"
        if not idb.is_dir():
            return names
        # Real Chromium: databases encoded inside LevelDB; also accept side-car dirs
        names.append("reduxPersistence")
        for p in idb.iterdir():
            if p.is_dir() and p.name.startswith("canvas-"):
                names.append(p.name)
            side = p / "records.json" if p.is_dir() else None
            if side and side.is_file() and p.name not in names:
                names.append(p.name)
        # Top-level side-car inventory
        inv = idb / "idb_sidecar.json"
        if inv.is_file():
            try:
                data = json.loads(inv.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for db in data.get("databases") or []:
                        if isinstance(db, str) and db not in names:
                            names.append(db)
            except (OSError, json.JSONDecodeError):
                pass
        return names

    def read_blob(self, ref: int | str) -> bytes | None:
        if self._blob_lookup is None:
            return None
        try:
            data = self._blob_lookup(int(ref))
        except (TypeError, ValueError):
            return None
        if isinstance(data, bytearray):
            return bytes(data)
        return data if isinstance(data, bytes) else None

    def iter_records(
        self,
        database: str | None = None,
        store: str | None = None,
        *,
        include_deleted: bool = True,
    ) -> Iterator[IdbRecord]:
        yield from self._iter_sidecar(database=database, store=store)
        if not self._leveldb.is_dir():
            return
        for raw, val, err in harvest_leveldb_dir(
            self._leveldb,
            blob_lookup=self._blob_lookup,
            include_deleted=include_deleted,
        ):
            key_s = raw.user_key.decode("utf-8", errors="replace")
            # Heuristic: Redux persist keys appear as UTF-8 substrings
            db_name = "reduxPersistence"
            store_name = "reduxPersistenceStore"
            if "canvas" in key_s.lower() or key_s.startswith("syncer."):
                db_name = "canvas"
                store_name = "canvas"
            if database is not None and db_name != database and database not in key_s:
                continue
            if store is not None and store_name != store:
                continue
            source = (
                "idb.leveldb.historical"
                if raw.state == "deleted"
                else "idb.leveldb"
            )
            clean: Any | None = None
            if val is not None:
                clean = sanitize(val)
            yield IdbRecord(
                database=db_name,
                store=store_name,
                key=key_s,
                value=clean,
                seq=raw.seq,
                state=raw.state,
                source=source,
                error=err,
            )

    def _iter_sidecar(
        self,
        *,
        database: str | None,
        store: str | None,
    ) -> Iterator[IdbRecord]:
        """Synthetic fixture path: IndexedDB/<db>/records.json list of {key,value,seq?,state?}."""
        idb = self.mirror / "IndexedDB"
        if not idb.is_dir():
            return
        for db_dir in idb.iterdir():
            if not db_dir.is_dir():
                continue
            side = db_dir / "records.json"
            if not side.is_file():
                continue
            db_name = db_dir.name
            if database is not None and db_name != database:
                continue
            try:
                rows = json.loads(side.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                st_name = str(row.get("store") or "default")
                if store is not None and st_name != store:
                    continue
                key = str(row.get("key") or "")
                val = row.get("value")
                clean = sanitize(val) if val is not None else None
                yield IdbRecord(
                    database=db_name,
                    store=st_name,
                    key=key,
                    value=clean,
                    seq=int(row["seq"]) if isinstance(row.get("seq"), int) else None,
                    state=str(row.get("state") or "live"),
                    source="idb.sidecar",
                    error=None,
                )
