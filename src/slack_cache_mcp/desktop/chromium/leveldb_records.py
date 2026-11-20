"""Harvest live + deleted LevelDB records (historical IDB values).

Uses vendored ccl RawLevelDb. Always operate on a mirror copy (skip LOCK).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import slack_cache_mcp._vendor  # noqa: F401
from ccl_chromium_reader.storage_formats.ccl_leveldb import KeyState, RawLevelDb

from slack_cache_mcp.desktop.chromium.idb_values import unwrap


@dataclass(frozen=True)
class RawLevelRecord:
    user_key: bytes
    value: bytes
    seq: int
    state: str  # live | deleted | unknown
    origin_file: str


def _state_name(state: KeyState) -> str:
    if state == KeyState.Live:
        return "live"
    if state == KeyState.Deleted:
        return "deleted"
    return "unknown"


def iter_raw_records(leveldb_dir: Path) -> Iterator[RawLevelRecord]:
    """Yield raw LevelDB records including deleted; dedupe by (user_key, seq)."""
    leveldb_dir = Path(leveldb_dir)
    if not leveldb_dir.is_dir():
        return
    seen: set[tuple[bytes, int]] = set()
    try:
        db = RawLevelDb(leveldb_dir)
    except Exception:  # noqa: BLE001 — torn/stub fixtures
        return
    try:
        for rec in db.iterate_records_raw():
            uk = bytes(rec.user_key)
            key = (uk, int(rec.seq))
            if key in seen:
                continue
            seen.add(key)
            yield RawLevelRecord(
                user_key=uk,
                value=bytes(rec.value) if rec.value is not None else b"",
                seq=int(rec.seq),
                state=_state_name(rec.state),
                origin_file=str(rec.origin_file),
            )
    except Exception:  # noqa: BLE001 — tolerate CRC/torn tails
        return
    finally:
        with contextlib.suppress(Exception):
            db.close()


def decode_record_value(
    value: bytes,
    *,
    blob_lookup: Any | None = None,
) -> tuple[Any | None, str | None]:
    """Unwrap an IDB value; return (obj, error)."""
    if not value:
        return None, "empty"
    try:
        return unwrap(value, blob_lookup), None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def harvest_leveldb_dir(
    leveldb_dir: Path,
    *,
    blob_lookup: Any | None = None,
    include_deleted: bool = True,
) -> list[tuple[RawLevelRecord, Any | None, str | None]]:
    """Return [(raw, decoded_or_None, error_or_None), ...]."""
    out: list[tuple[RawLevelRecord, Any | None, str | None]] = []
    for raw in iter_raw_records(leveldb_dir):
        if not include_deleted and raw.state == "deleted":
            continue
        if raw.state == "deleted" and not raw.value:
            out.append((raw, None, "deleted_no_value"))
            continue
        val, err = decode_record_value(raw.value, blob_lookup=blob_lookup)
        out.append((raw, val, err))
    return out
