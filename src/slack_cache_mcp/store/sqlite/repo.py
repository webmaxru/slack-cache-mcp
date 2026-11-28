"""SQLite ArchiveRepository implementation."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from slack_cache_mcp.domain.models import (
    CanvasDoc,
    Channel,
    Draft,
    FileMeta,
    MessageObservation,
    ReactionSet,
    SavedItem,
    User,
    Workspace,
)
from slack_cache_mcp.ingest.projection import (
    VersionCandidate,
    choose_current,
    deleted_state_for,
    max_merge_thread_meta,
)

SCHEMA_VERSION = "5"
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_XOX_RE = re.compile(rb"xox[a-z]-", re.I)


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ts_num(ts: str) -> float:
    try:
        return float(ts)
    except ValueError:
        return 0.0


# Sentinel field clock for migrated values with unknown provenance. Stable across
# later unrelated row-HLC bumps; remote observations never overwrite it.
_UNKNOWN_FIELD_HLC = "*"
# Bumped by directory clock repair; peers with a lower epoch have their field
# clocks ignored, and a higher epoch force-applies values (receiver-side repair).
_DIRECTORY_CLOCK_EPOCH_META = "directory_clock_epoch"
_DIRECTORY_CLOCK_EPOCH_REPAIR = 1
# Internal field-map key: last winning clock_repair HLC (not a Slack field).
_REPAIR_HLC_KEY = "_repair_hlc"


def _hlc_dominates(incoming: str, existing: str | None) -> bool:
    """True when ``incoming`` should win field-level LWW (tie → device_id lex)."""
    if not existing:
        return True
    if existing == _UNKNOWN_FIELD_HLC or incoming == _UNKNOWN_FIELD_HLC:
        return False
    from slack_cache_mcp.ingest.hlc import parse_hlc

    try:
        return parse_hlc(incoming) >= parse_hlc(existing)
    except ValueError:
        return incoming >= existing


def _load_field_hlcs(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, str):
            out[key] = value
    return out


def _dump_field_hlcs(field_hlcs: dict[str, str]) -> str:
    return json.dumps(field_hlcs, sort_keys=True, separators=(",", ":"))


def _real_field_hlc(clock: str | None) -> str | None:
    """Return ``clock`` when it is a trustworthy (non-sentinel) field HLC."""
    if not clock or clock == _UNKNOWN_FIELD_HLC:
        return None
    return clock


def _merge_sparse_field(
    existing_val: Any,
    existing_field_hlc: str | None,
    incoming_val: Any,
    incoming_hlc: str | None,
    *,
    replace_unknown: bool = False,
) -> tuple[Any, str | None]:
    """Per-field LWW: null incoming is a no-op; non-null keeps its own clock.

    ``incoming_hlc is None`` = unknown incoming (sparse-fill nulls only).
    ``*`` = stable unknown local provenance; remotes cannot overwrite; local
    writes pass ``replace_unknown=True`` to set a real clock.
    """
    if incoming_val is None:
        return existing_val, existing_field_hlc
    if existing_val is None:
        return incoming_val, incoming_hlc if incoming_hlc is not None else _UNKNOWN_FIELD_HLC
    pinned = existing_field_hlc if existing_field_hlc is not None else _UNKNOWN_FIELD_HLC
    if pinned == _UNKNOWN_FIELD_HLC:
        # Convergent: any real HLC wins over unknown (local or peer). Two unknowns
        # pick the lexicographically greater value for order-independence.
        if incoming_hlc and incoming_hlc != _UNKNOWN_FIELD_HLC:
            return incoming_val, incoming_hlc
        if str(incoming_val) > str(existing_val):
            return incoming_val, _UNKNOWN_FIELD_HLC
        return existing_val, _UNKNOWN_FIELD_HLC
    if incoming_hlc is None or incoming_hlc == _UNKNOWN_FIELD_HLC:
        return existing_val, pinned
    if _hlc_dominates(incoming_hlc, pinned):
        return incoming_val, incoming_hlc
    return existing_val, pinned


def _filter_clause(filters: dict[str, Any], *, alias: str = "m") -> tuple[str, list[Any]]:
    """Shared WHERE fragments for FTS, trigram, and filter-only search."""
    sql = ""
    params: list[Any] = []
    channel_ids = filters.get("channel_ids") or (
        [filters["channel_id"]] if filters.get("channel_id") else None
    )
    user_ids = filters.get("user_ids") or (
        [filters["user_id"]] if filters.get("user_id") else None
    )
    if channel_ids:
        placeholders = ",".join("?" for _ in channel_ids)
        sql += f" AND {alias}.channel_id IN ({placeholders})"
        params.extend(channel_ids)
    if user_ids:
        placeholders = ",".join("?" for _ in user_ids)
        sql += f" AND {alias}.user_id IN ({placeholders})"
        params.extend(user_ids)
    if filters.get("workspace_key"):
        sql += f" AND {alias}.workspace_key=?"
        params.append(filters["workspace_key"])
    deleted_mode = filters.get("deleted_mode", "exclude")
    if deleted_mode == "exclude":
        sql += f" AND {alias}.deleted_state='none'"
    elif deleted_mode == "only":
        sql += f" AND {alias}.deleted_state!='none'"
    # deleted_mode == "include" → no predicate
    if filters.get("after") is not None:
        sql += f" AND {alias}.ts_num >= ?"
        params.append(float(filters["after"]))
    if filters.get("before") is not None:
        sql += f" AND {alias}.ts_num < ?"
        params.append(float(filters["before"]))
    if filters.get("has_files"):
        sql += f" AND {alias}.has_files=1"
    if filters.get("has_links"):
        sql += f" AND {alias}.has_links=1"
    if filters.get("is_thread"):
        sql += (
            f" AND ({alias}.thread_ts IS NOT NULL OR "
            f"COALESCE({alias}.reply_count, 0) > 0)"
        )
    if filters.get("is_reply"):
        sql += f" AND {alias}.is_reply=1"
    if filters.get("is_edited"):
        sql += f" AND {alias}.edited_ts IS NOT NULL"
    if filters.get("has_reaction"):
        sql += (
            f" AND EXISTS (SELECT 1 FROM reactions rx "
            f"WHERE rx.channel_id={alias}.channel_id AND rx.ts={alias}.ts)"
        )
    with_ids = filters.get("with_user_ids") or []
    if with_ids:
        # Participant: author OR another message in the same thread by that user.
        placeholders = ",".join("?" for _ in with_ids)
        sql += (
            f" AND ("
            f" {alias}.user_id IN ({placeholders})"
            f" OR EXISTS ("
            f"   SELECT 1 FROM messages w"
            f"   WHERE w.channel_id={alias}.channel_id"
            f"     AND w.user_id IN ({placeholders})"
            f"     AND ("
            f"       COALESCE({alias}.thread_ts, {alias}.ts) = COALESCE(w.thread_ts, w.ts)"
            f"     )"
            f" )"
            f")"
        )
        params.extend(with_ids)
        params.extend(with_ids)
    return sql, params


class SqliteRepository:
    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = Path(path)
        # Canonicalize so symlink aliases share lock identity within this build.
        with contextlib.suppress(OSError):
            if self.path.exists():
                self.path = self.path.resolve()
        self.read_only = read_only
        if read_only:
            if not self.path.exists():
                raise FileNotFoundError(self.path)
            from urllib.parse import quote

            # Quote so resolved targets containing `?`/`#` stay in the path.
            encoded = quote(self.path.as_posix(), safe="/:")
            uri = f"file:{encoded}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        # Best-effort sqlite-vec; lexical search still works if unavailable.
        from slack_cache_mcp.store.sqlite.vec import try_load_sqlite_vec

        self._vec_ready = try_load_sqlite_vec(self._conn)
        if not read_only:
            self.ensure_schema()
        # Defer docs_vec creation until embedder dimension is known (worker).
        # Crash-released OS file locks for embedding rebuilds (see acquire_*).
        self._embed_lock_fhs: list[TextIO] = []
        self._embed_lock_owner: str | None = None

    def close(self) -> None:
        if self._embed_lock_fhs:
            with contextlib.suppress(Exception):
                self._release_embed_file_lock()
        self._conn.close()

    def ensure_schema(self) -> None:
        sql = _SCHEMA_PATH.read_text(encoding="utf-8")
        self._conn.executescript(sql)
        self._migrate_schema()
        self._conn.execute(
            """
            INSERT INTO meta(key, value) VALUES ('schema_version', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (SCHEMA_VERSION,),
        )
        self._conn.commit()

    def _migrate_schema(self) -> None:
        """Bring older on-disk DBs up to SCHEMA_VERSION (additive only)."""
        # state_generations.vault_path existed in M1 schema.sql; still guard partial DBs.
        cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(state_generations)").fetchall()
        }
        if cols and "vault_path" not in cols:
            self._conn.execute("ALTER TABLE state_generations ADD COLUMN vault_path TEXT")
        # suspected_streaks is CREATE IF NOT EXISTS in schema.sql; no-op if present.
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS suspected_streaks (
              channel_id TEXT NOT NULL,
              ts TEXT NOT NULL,
              streak INTEGER NOT NULL,
              last_state_sha256 TEXT,
              last_seen TEXT NOT NULL,
              PRIMARY KEY (channel_id, ts)
            ) STRICT
            """
        )
        # M3 tables (additive; schema.sql CREATE IF NOT EXISTS also covers fresh DBs)
        for ddl in (
            """
            CREATE TABLE IF NOT EXISTS reactions (
              channel_id TEXT NOT NULL, ts TEXT NOT NULL, emoji TEXT NOT NULL,
              count INTEGER NOT NULL, users_json TEXT, observed_hlc TEXT NOT NULL,
              PRIMARY KEY (channel_id, ts, emoji)
            ) STRICT
            """,
            """
            CREATE TABLE IF NOT EXISTS files (
              file_id TEXT PRIMARY KEY, workspace_key TEXT, user_id TEXT,
              name TEXT, title TEXT, mimetype TEXT, filetype TEXT, size INTEGER,
              created REAL, preview_text TEXT, local_path TEXT, local_sha256 TEXT,
              thumb_vault_path TEXT, ocr_text TEXT, raw_json TEXT NOT NULL,
              first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, updated_hlc TEXT NOT NULL
            ) STRICT
            """,
            """
            CREATE TABLE IF NOT EXISTS message_files (
              channel_id TEXT NOT NULL, ts TEXT NOT NULL, file_id TEXT NOT NULL,
              PRIMARY KEY (channel_id, ts, file_id)
            ) STRICT
            """,
            """
            CREATE TABLE IF NOT EXISTS saved_items (
              workspace_key TEXT NOT NULL, item_type TEXT NOT NULL, item_id TEXT NOT NULL,
              channel_id TEXT, ts TEXT, state TEXT, date_created REAL, date_due REAL,
              raw_json TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
              PRIMARY KEY (workspace_key, item_type, item_id)
            ) STRICT
            """,
            """
            CREATE TABLE IF NOT EXISTS canvases (
              canvas_id TEXT PRIMARY KEY, workspace_key TEXT, channel_id TEXT, title TEXT,
              text TEXT NOT NULL, raw_json TEXT NOT NULL, content_sha256 TEXT NOT NULL,
              first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
            ) STRICT
            """,
            """
            CREATE TABLE IF NOT EXISTS canvas_versions (
              canvas_id TEXT NOT NULL, content_sha256 TEXT NOT NULL, text TEXT NOT NULL,
              first_seen TEXT NOT NULL, PRIMARY KEY (canvas_id, content_sha256)
            ) STRICT
            """,
            """
            CREATE TABLE IF NOT EXISTS drafts (
              workspace_key TEXT NOT NULL, draft_id TEXT NOT NULL, channel_id TEXT,
              thread_ts TEXT, text TEXT NOT NULL, last_updated REAL, raw_json TEXT NOT NULL,
              first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
              PRIMARY KEY (workspace_key, draft_id)
            ) STRICT
            """,
            """
            CREATE TABLE IF NOT EXISTS source_yield (
              snapshot_id TEXT NOT NULL, source_name TEXT NOT NULL,
              messages INTEGER NOT NULL DEFAULT 0, files INTEGER NOT NULL DEFAULT 0,
              canvases INTEGER NOT NULL DEFAULT 0, reactions INTEGER NOT NULL DEFAULT 0,
              saved_items INTEGER NOT NULL DEFAULT 0, drafts INTEGER NOT NULL DEFAULT 0,
              cache_entries INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0,
              extras_json TEXT NOT NULL DEFAULT '{}',
              PRIMARY KEY (snapshot_id, source_name)
            ) STRICT
            """,
            """
            CREATE TABLE IF NOT EXISTS search_docs (
              doc_id INTEGER PRIMARY KEY,
              kind TEXT NOT NULL,
              workspace_key TEXT NOT NULL,
              channel_id TEXT,
              anchor_ts TEXT,
              start_ts TEXT,
              end_ts TEXT,
              text TEXT NOT NULL,
              text_sha256 TEXT NOT NULL,
              embedded_model TEXT,
              embedded_at TEXT,
              message_rowid INTEGER,
              UNIQUE (kind, workspace_key, channel_id, anchor_ts, start_ts, end_ts)
            ) STRICT
            """,
            """
            CREATE TABLE IF NOT EXISTS doc_embeddings (
              doc_id INTEGER PRIMARY KEY,
              dim INTEGER NOT NULL,
              embedding BLOB NOT NULL,
              model_id TEXT NOT NULL
            ) STRICT
            """,
        ):
            self._conn.execute(ddl)
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS search_docs_pending
              ON search_docs(embedded_model) WHERE embedded_model IS NULL
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS search_docs_channel
              ON search_docs(channel_id, anchor_ts)
            """
        )
        # M5 sync cursors (additive)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_cursors (
              peer TEXT NOT NULL,
              direction TEXT NOT NULL,
              cursor TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY (peer, direction)
            ) STRICT
            """
        )
        # Per-field HLC maps for sparse directory LWW (additive).
        for table in ("workspaces", "channels", "users"):
            cols = {
                row[1] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if cols and "field_hlcs_json" not in cols:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN field_hlcs_json TEXT NOT NULL DEFAULT '{{}}'"
                )
        self._pin_unknown_directory_field_hlcs()

    def directory_clock_epoch(self) -> int:
        raw = self.get_meta(_DIRECTORY_CLOCK_EPOCH_META)
        if raw is None:
            return 0
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    def set_directory_clock_epoch(self, epoch: int, *, commit: bool = False) -> None:
        self.set_meta(_DIRECTORY_CLOCK_EPOCH_META, str(int(epoch)), commit=commit)

    def invalidate_directory_field_clocks(self, *, commit: bool = False) -> None:
        """Pin every non-null directory field to ``*`` (epoch repair)."""
        specs: list[tuple[str, str, tuple[str, ...]]] = [
            (
                "workspaces",
                "workspace_key",
                ("team_id", "enterprise_id", "name", "domain", "self_user_id"),
            ),
            (
                "channels",
                "channel_id",
                ("workspace_key", "context_team_id", "name", "topic", "purpose", "im_user_id"),
            ),
            (
                "users",
                "user_id",
                ("team_id", "name", "real_name", "display_name", "title", "tz"),
            ),
        ]
        for table, pk, fields in specs:
            for row in self._conn.execute(f"SELECT * FROM {table}").fetchall():
                repaired = {
                    field: _UNKNOWN_FIELD_HLC for field in fields if row[field] is not None
                }
                self._conn.execute(
                    f"UPDATE {table} SET field_hlcs_json=? WHERE {pk}=?",
                    (_dump_field_hlcs(repaired), row[pk]),
                )
        if commit:
            self._conn.commit()

    def _pin_unknown_directory_field_hlcs(self) -> None:
        """Repair directory provenance via value recovery, then epoch bump.

        1. Restore each field's value from the dominating ``(value, clock)`` pair
           in ``change_log`` (value and clock stay paired for selection only).
        2. Pin every non-null field clock to ``*`` — historical clocks are not
           trusted as provenance (they may be fabricated).
        3. Bump ``directory_clock_epoch`` and mark ``v8-pending-outbox-purge`` so
           the next export rebuilds local directory outbox with ``clock_repair``.
        """
        policy = self.get_meta("field_hlcs_policy")
        if policy in (
            "v8-epoch-repair",
            "v8-pending-outbox-purge",
        ):
            return
        specs: list[tuple[str, str, str, tuple[str, ...]]] = [
            (
                "workspaces",
                "workspace",
                "workspace_key",
                ("team_id", "enterprise_id", "name", "domain", "self_user_id"),
            ),
            (
                "channels",
                "channel",
                "channel_id",
                ("workspace_key", "context_team_id", "name", "topic", "purpose", "im_user_id"),
            ),
            (
                "users",
                "user",
                "user_id",
                ("team_id", "name", "real_name", "display_name", "title", "tz"),
            ),
        ]
        # Best (value, clock) per field from outbox history — clock and value stay paired.
        # Prefer pairs that already match the current table value (applied state).
        recovered: dict[tuple[str, str, str], tuple[Any, str]] = {}
        for entity in ("workspace", "channel", "user"):
            for row in self._conn.execute(
                "SELECT entity_key, payload_json FROM change_log WHERE entity=?",
                (entity,),
            ).fetchall():
                try:
                    payload = json.loads(row["payload_json"])
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                field_hlcs = payload.get("field_hlcs")
                if not isinstance(field_hlcs, dict):
                    continue
                for field, clock in field_hlcs.items():
                    if not isinstance(field, str) or not isinstance(clock, str):
                        continue
                    real = _real_field_hlc(clock)
                    if real is None:
                        continue
                    value = payload.get(field)
                    if value is None:
                        continue
                    key = (entity, str(row["entity_key"]), field)
                    prev = recovered.get(key)
                    if prev is None or _hlc_dominates(real, prev[1]):
                        recovered[key] = (value, real)

        for table, entity, pk, fields in specs:
            for row in self._conn.execute(f"SELECT * FROM {table}").fetchall():
                repaired: dict[str, str] = {}
                updates: dict[str, Any] = {}
                for field in fields:
                    table_val = row[field]
                    pair = recovered.get((entity, str(row[pk]), field))
                    if pair is not None:
                        value, _clock = pair
                        if table_val != value:
                            updates[field] = value
                            table_val = value
                    if table_val is not None:
                        # Never re-export historical clocks as trusted provenance.
                        repaired[field] = _UNKNOWN_FIELD_HLC
                if updates:
                    assignments = ", ".join(f"{col}=?" for col in updates)
                    self._conn.execute(
                        f"UPDATE {table} SET {assignments}, field_hlcs_json=? WHERE {pk}=?",
                        (*updates.values(), _dump_field_hlcs(repaired), row[pk]),
                    )
                else:
                    self._conn.execute(
                        f"UPDATE {table} SET field_hlcs_json=? WHERE {pk}=?",
                        (_dump_field_hlcs(repaired), row[pk]),
                    )
        current_epoch = self.directory_clock_epoch()
        if current_epoch < _DIRECTORY_CLOCK_EPOCH_REPAIR:
            self.set_directory_clock_epoch(_DIRECTORY_CLOCK_EPOCH_REPAIR, commit=False)
        self.set_meta("field_hlcs_policy", "v8-pending-outbox-purge", commit=False)

    def upsert_device(
        self, device_id: str, label: str, *, public_key: str | None = None
    ) -> None:
        now = _utc_now()
        self._conn.execute(
            """
            INSERT INTO devices(device_id, label, public_key, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
              label=excluded.label,
              public_key=COALESCE(excluded.public_key, devices.public_key)
            """,
            (device_id, label, public_key, now),
        )
        self._conn.commit()

    def list_devices(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT device_id, label, public_key, created_at FROM devices ORDER BY device_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_sync_cursor(self, peer: str, direction: str) -> str | None:
        row = self._conn.execute(
            "SELECT cursor FROM sync_cursors WHERE peer=? AND direction=?",
            (peer, direction),
        ).fetchone()
        return str(row["cursor"]) if row else None

    def set_sync_cursor(self, peer: str, direction: str, cursor: str) -> None:
        self._conn.execute(
            """
            INSERT INTO sync_cursors(peer, direction, cursor, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(peer, direction) DO UPDATE SET
              cursor=excluded.cursor,
              updated_at=excluded.updated_at
            """,
            (peer, direction, cursor, _utc_now()),
        )
        self._conn.commit()

    def cas_sync_cursor(
        self,
        peer: str,
        direction: str,
        *,
        expected: str,
        new: str,
    ) -> bool:
        """Compare-and-swap a sync cursor under ``BEGIN IMMEDIATE``.

        Returns True when the cursor was updated from ``expected`` to ``new``.
        Missing rows are treated as ``expected == "0"``.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT cursor FROM sync_cursors WHERE peer=? AND direction=?",
                (peer, direction),
            ).fetchone()
            current = str(row["cursor"]) if row else "0"
            if current != expected:
                self._conn.execute("ROLLBACK")
                return False
            now = _utc_now()
            self._conn.execute(
                """
                INSERT INTO sync_cursors(peer, direction, cursor, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(peer, direction) DO UPDATE SET
                  cursor=excluded.cursor,
                  updated_at=excluded.updated_at
                """,
                (peer, direction, new, now),
            )
            self._conn.commit()
            return True
        except Exception:
            with contextlib.suppress(Exception):
                self._conn.execute("ROLLBACK")
            raise

    def bump_sync_cursor_max(self, peer: str, direction: str, cursor: str) -> str:
        """Atomically keep the numeric max of the existing cursor and ``cursor``."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT cursor FROM sync_cursors WHERE peer=? AND direction=?",
                (peer, direction),
            ).fetchone()
            prev = int(str(row["cursor"])) if row else 0
            nxt = max(prev, int(cursor))
            now = _utc_now()
            self._conn.execute(
                """
                INSERT INTO sync_cursors(peer, direction, cursor, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(peer, direction) DO UPDATE SET
                  cursor=excluded.cursor,
                  updated_at=excluded.updated_at
                """,
                (peer, direction, str(nxt), now),
            )
            self._conn.commit()
            return str(nxt)
        except Exception:
            with contextlib.suppress(Exception):
                self._conn.execute("ROLLBACK")
            raise

    def list_sync_cursors(self) -> list[dict[str, str]]:
        """Return push/pull watermarks (counts/ids only — no message text)."""
        rows = self._conn.execute(
            """
            SELECT peer, direction, cursor, updated_at
            FROM sync_cursors
            ORDER BY peer, direction
            """
        ).fetchall()
        return [
            {
                "peer": str(r["peer"]),
                "direction": str(r["direction"]),
                "cursor": str(r["cursor"]),
                "updated_at": str(r["updated_at"]),
            }
            for r in rows
        ]

    def reset_push_cursors(self) -> int:
        """Reset every push cursor so the next export re-sends full history."""
        cur = self._conn.execute(
            """
            UPDATE sync_cursors SET cursor='0', updated_at=?
            WHERE direction='push'
            """,
            (_utc_now(),),
        )
        # Ensure the default folder peer exists even if never exported.
        self._conn.execute(
            """
            INSERT INTO sync_cursors(peer, direction, cursor, updated_at)
            VALUES ('folder', 'push', '0', ?)
            ON CONFLICT(peer, direction) DO UPDATE SET
              cursor='0', updated_at=excluded.updated_at
            """,
            (_utc_now(),),
        )
        self._conn.commit()
        return int(cur.rowcount or 0)

    def change_log_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) AS c FROM change_log").fetchone()["c"])

    def max_local_hlc(self, device_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT hlc FROM change_log WHERE device_id=? ORDER BY seq DESC LIMIT 1",
            (device_id,),
        ).fetchone()
        return str(row["hlc"]) if row else None

    def has_change_log(
        self,
        *,
        device_id: str,
        entity: str,
        entity_key: str,
        payload_sha256: str,
    ) -> bool:
        row = self._conn.execute(
            """
            SELECT 1 FROM change_log
            WHERE device_id=? AND entity=? AND entity_key=? AND payload_sha256=?
            """,
            (device_id, entity, entity_key, payload_sha256),
        ).fetchone()
        return row is not None

    def insert_change_log_row(
        self,
        *,
        hlc: str,
        device_id: str,
        entity: str,
        entity_key: str,
        op: str,
        payload: dict[str, Any],
        payload_sha256: str,
        commit: bool = True,
    ) -> bool:
        """Insert outbox row. Returns True if a new row was written."""
        from slack_cache_mcp.desktop.sanitize import sanitize

        clean = sanitize(payload)
        if not isinstance(clean, dict):
            raise TypeError("change_log payload must sanitize to a dict")
        payload_json = json.dumps(clean, sort_keys=True, ensure_ascii=False)
        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO change_log(
              hlc, device_id, entity, entity_key, op, payload_json, payload_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (hlc, device_id, entity, entity_key, op, payload_json, payload_sha256),
        )
        if commit:
            self._conn.commit()
        return cur.rowcount > 0

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT device_id, label, public_key, created_at FROM devices WHERE device_id=?",
            (device_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str, *, commit: bool = True) -> None:
        self._conn.execute(
            """
            INSERT INTO meta(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )
        if commit:
            self._conn.commit()

    def get_hlc_watermark(self) -> str | None:
        return self.get_meta("hlc_watermark")

    def set_hlc_watermark(self, hlc: str, *, commit: bool = True) -> None:
        """Monotonic HLC watermark with an atomic compare-and-swap.

        ``BEGIN IMMEDIATE`` serializes concurrent snapshot/import writers on
        separate connections so a stale read cannot overwrite a newer watermark.
        """
        from slack_cache_mcp.ingest.hlc import parse_hlc

        own_tx = commit
        started = False
        if own_tx and not self._conn.in_transaction:
            self._conn.execute("BEGIN IMMEDIATE")
            started = True
        try:
            current = self.get_hlc_watermark()
            if current is not None:
                try:
                    if parse_hlc(hlc) <= parse_hlc(current):
                        if started:
                            self._conn.commit()
                        return
                except ValueError:
                    pass
            self.set_meta("hlc_watermark", hlc, commit=False)
            if started or own_tx:
                self._conn.commit()
        except Exception:
            if started or own_tx:
                with contextlib.suppress(Exception):
                    self._conn.rollback()
            raise

    def delete_incomplete_local_message_outbox(self, device_id: str) -> int:
        """Drop legacy stub message_version outbox rows missing full content fields."""
        rows = self._conn.execute(
            """
            SELECT seq, payload_json FROM change_log
            WHERE device_id=? AND entity='message_version'
            """,
            (device_id,),
        ).fetchall()
        deleted = 0
        for r in rows:
            try:
                payload = json.loads(r["payload_json"])
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if "text" in payload and "raw" in payload and "workspace_key" in payload:
                continue
            self._conn.execute("DELETE FROM change_log WHERE seq=?", (r["seq"],))
            deleted += 1
        if deleted:
            self._conn.commit()
        return deleted

    def _has_entity_outbox(self, device_id: str, entity: str, entity_key: str) -> bool:
        row = self._conn.execute(
            """
            SELECT 1 FROM change_log
            WHERE device_id=? AND entity=? AND entity_key=?
            LIMIT 1
            """,
            (device_id, entity, entity_key),
        ).fetchone()
        return row is not None

    def _outbox_covers_fields(
        self,
        device_id: str,
        entity: str,
        entity_key: str,
        required: dict[str, Any],
    ) -> bool:
        """True when union of outbox payloads already carries every non-null required field."""
        rows = self._conn.execute(
            """
            SELECT payload_json FROM change_log
            WHERE device_id=? AND entity=? AND entity_key=?
            """,
            (device_id, entity, entity_key),
        ).fetchall()
        if not rows:
            return False
        merged: dict[str, Any] = {}
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            for key, value in payload.items():
                if value is not None:
                    merged[key] = value
        for key, value in required.items():
            if value is not None and merged.get(key) is None:
                return False
        return True

    def backfill_outbox(self, *, device_id: str, hlc: str) -> dict[str, int]:
        """Write missing sanitized outbox facts from archive tables (M4→M5 upgrade).

        Preserves each row's original ``updated_hlc`` (falling back to ``hlc`` only
        when absent) so backfill cannot reauthor stale directory facts ahead of
        peer observations. Skips entity keys that already have any outbox row.
        """
        from slack_cache_mcp.domain.models import Channel, User, Workspace

        policy = self.get_meta("field_hlcs_policy")
        emit_clock_repair = policy in (
            "v8-pending-outbox-purge",
            "v7-pending-outbox-purge",
            "v6-pending-outbox-purge",
            "v5-pending-outbox-purge",
        )
        if emit_clock_repair:
            # Drop fabricated local directory facts; peer markers stay for idempotency.
            # Keep policy as *-pending until rebuild finishes so a crash retries with
            # clock_repair authority instead of ordinary backfill.
            self._conn.execute(
                """
                DELETE FROM change_log
                WHERE device_id=? AND entity IN ('workspace', 'channel', 'user')
                """,
                (device_id,),
            )
            self._conn.execute(
                """
                UPDATE sync_cursors SET cursor='0', updated_at=?
                WHERE direction='push'
                """,
                (_utc_now(),),
            )
            self._conn.execute(
                """
                INSERT INTO sync_cursors(peer, direction, cursor, updated_at)
                VALUES ('folder', 'push', '0', ?)
                ON CONFLICT(peer, direction) DO UPDATE SET
                  cursor='0', updated_at=excluded.updated_at
                """,
                (_utc_now(),),
            )
            self._conn.commit()

        self.delete_incomplete_local_message_outbox(device_id)
        counts = {
            "workspaces": 0,
            "channels": 0,
            "users": 0,
            "message_versions": 0,
            "sightings": 0,
        }

        def _row_hlc(row: Any, fallback: str) -> str:
            try:
                value = row["updated_hlc"]
            except (KeyError, IndexError, TypeError):
                value = None
            return str(value) if value else fallback

        for r in self._conn.execute("SELECT * FROM workspaces").fetchall():
            required = {
                "team_id": r["team_id"],
                "enterprise_id": r["enterprise_id"],
                "name": r["name"],
                "domain": r["domain"],
                "self_user_id": r["self_user_id"],
            }
            if self._outbox_covers_fields(
                device_id, "workspace", r["workspace_key"], required
            ):
                continue
            ws = Workspace(
                workspace_key=r["workspace_key"],
                team_id=r["team_id"],
                enterprise_id=r["enterprise_id"],
                name=r["name"],
                domain=r["domain"],
                self_user_id=r["self_user_id"],
                self_is_guest=bool(r["self_is_guest"]),
                raw=json.loads(r["raw_json"] or "{}"),
            )
            # Export only known clocks — never invent row HLC for unclocked fields.
            field_hlcs = {
                k: v
                for k, v in _load_field_hlcs(r["field_hlcs_json"]).items()
                if v and v != _UNKNOWN_FIELD_HLC and not k.startswith("_")
            }
            if self.append_entity_change(
                hlc=_row_hlc(r, hlc),
                device_id=device_id,
                entity="workspace",
                entity_key=ws.workspace_key,
                payload={
                    "workspace_key": ws.workspace_key,
                    "team_id": ws.team_id,
                    "enterprise_id": ws.enterprise_id,
                    "name": ws.name,
                    "domain": ws.domain,
                    "self_user_id": ws.self_user_id,
                    "self_is_guest": ws.self_is_guest,
                    "raw": ws.raw,
                    "first_seen": r["first_seen"],
                    "last_seen": r["last_seen"],
                    "field_hlcs": field_hlcs,
                    "clock_epoch": self.directory_clock_epoch(),
                    **({"clock_repair": True} if emit_clock_repair else {}),
                },
            ):
                counts["workspaces"] += 1
        for r in self._conn.execute("SELECT * FROM channels").fetchall():
            required = {
                "workspace_key": r["workspace_key"],
                "context_team_id": r["context_team_id"],
                "name": r["name"],
                "topic": r["topic"],
                "purpose": r["purpose"],
                "im_user_id": r["im_user_id"],
            }
            ws_keys = [
                row["workspace_key"]
                for row in self._conn.execute(
                    "SELECT workspace_key FROM channel_workspaces WHERE channel_id=?",
                    (r["channel_id"],),
                ).fetchall()
            ]
            alias_rows = self._conn.execute(
                "SELECT name, first_seen FROM channel_aliases WHERE channel_id=?",
                (r["channel_id"],),
            ).fetchall()
            aliases = [
                {"name": row["name"], "first_seen": row["first_seen"]} for row in alias_rows
            ]
            # Union coverage: every workspace/alias (with earliest first_seen) must appear.
            covered_ws: set[str] = set()
            covered_alias_seen: dict[str, str] = {}
            for prow in self._conn.execute(
                """
                SELECT payload_json FROM change_log
                WHERE device_id=? AND entity='channel' AND entity_key=?
                """,
                (device_id, r["channel_id"]),
            ).fetchall():
                try:
                    payload = json.loads(prow["payload_json"])
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                if isinstance(payload.get("workspace_key"), str):
                    covered_ws.add(payload["workspace_key"])
                for wk in payload.get("workspace_keys") or []:
                    if isinstance(wk, str):
                        covered_ws.add(wk)
                for alias in payload.get("alias_records") or []:
                    if isinstance(alias, dict) and isinstance(alias.get("name"), str):
                        name = alias["name"]
                        seen = str(alias.get("first_seen") or "")
                        prev = covered_alias_seen.get(name)
                        if prev is None or (seen and (not prev or seen < prev)):
                            covered_alias_seen[name] = seen
            # Dual-format coverage: require string aliases list AND earliest first_seen.
            has_string_aliases = False
            for prow2 in self._conn.execute(
                """
                SELECT payload_json FROM change_log
                WHERE device_id=? AND entity='channel' AND entity_key=?
                """,
                (device_id, r["channel_id"]),
            ).fetchall():
                try:
                    p2 = json.loads(prow2["payload_json"])
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(p2, dict):
                    continue
                if any(isinstance(a, str) for a in (p2.get("aliases") or [])):
                    has_string_aliases = True
                    break
            aliases_covered = has_string_aliases or not aliases
            for alias in aliases:
                name = alias["name"]
                want = str(alias["first_seen"] or "")
                got = covered_alias_seen.get(name)
                if got is None or (want and (not got or got > want)):
                    aliases_covered = False
                    break
            if (
                self._outbox_covers_fields(device_id, "channel", r["channel_id"], required)
                and set(ws_keys).issubset(covered_ws)
                and aliases_covered
            ):
                continue
            ch = Channel(
                channel_id=r["channel_id"],
                workspace_key=r["workspace_key"],
                context_team_id=r["context_team_id"],
                name=r["name"],
                kind=r["kind"],
                is_ext_shared=bool(r["is_ext_shared"]),
                is_archived=bool(r["is_archived"]),
                is_member=bool(r["is_member"]),
                topic=r["topic"],
                purpose=r["purpose"],
                im_user_id=r["im_user_id"],
                raw=json.loads(r["raw_json"] or "{}"),
            )
            field_hlcs = {
                k: v
                for k, v in _load_field_hlcs(r["field_hlcs_json"]).items()
                if v and v != _UNKNOWN_FIELD_HLC and not k.startswith("_")
            }
            if self.append_entity_change(
                hlc=_row_hlc(r, hlc),
                device_id=device_id,
                entity="channel",
                entity_key=ch.channel_id,
                payload={
                    "channel_id": ch.channel_id,
                    "workspace_key": ch.workspace_key,
                    "workspace_keys": ws_keys,
                    "aliases": [a["name"] for a in aliases],
                    "alias_records": aliases,
                    "context_team_id": ch.context_team_id,
                    "name": ch.name,
                    "kind": ch.kind,
                    "is_ext_shared": ch.is_ext_shared,
                    "is_archived": ch.is_archived,
                    "is_member": ch.is_member,
                    "topic": ch.topic,
                    "purpose": ch.purpose,
                    "im_user_id": ch.im_user_id,
                    "raw": ch.raw,
                    "first_seen": r["first_seen"],
                    "last_seen": r["last_seen"],
                    "field_hlcs": field_hlcs,
                    "clock_epoch": self.directory_clock_epoch(),
                    **({"clock_repair": True} if emit_clock_repair else {}),
                },
            ):
                counts["channels"] += 1
        for r in self._conn.execute("SELECT * FROM users").fetchall():
            required = {
                "name": r["name"],
                "real_name": r["real_name"],
                "display_name": r["display_name"],
                "title": r["title"],
                "tz": r["tz"],
                "team_id": r["team_id"],
            }
            ws_keys = [
                row["workspace_key"]
                for row in self._conn.execute(
                    "SELECT workspace_key FROM user_workspaces WHERE user_id=?",
                    (r["user_id"],),
                ).fetchall()
            ]
            required_with_assoc = {
                **required,
                "workspace_keys": ws_keys or None,
            }
            if self._outbox_covers_fields(
                device_id, "user", r["user_id"], required_with_assoc
            ):
                # Association coverage: every workspace_key must appear in some payload.
                covered_user_ws: set[str] = set()
                for prow in self._conn.execute(
                    """
                    SELECT payload_json FROM change_log
                    WHERE device_id=? AND entity='user' AND entity_key=?
                    """,
                    (device_id, r["user_id"]),
                ).fetchall():
                    try:
                        payload = json.loads(prow["payload_json"])
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if isinstance(payload.get("workspace_key"), str):
                        covered_user_ws.add(payload["workspace_key"])
                    for wk in payload.get("workspace_keys") or []:
                        if isinstance(wk, str):
                            covered_user_ws.add(wk)
                if set(ws_keys).issubset(covered_user_ws):
                    continue
            user = User(
                user_id=r["user_id"],
                team_id=r["team_id"],
                name=r["name"],
                real_name=r["real_name"],
                display_name=r["display_name"],
                title=r["title"],
                email=None,
                is_bot=bool(r["is_bot"]),
                is_deleted=bool(r["is_deleted"]),
                is_restricted=bool(r["is_restricted"]),
                is_ultra_restricted=bool(r["is_ultra_restricted"]),
                is_external=bool(r["is_external"]),
                tz=r["tz"],
                raw=json.loads(r["raw_json"] or "{}"),
                workspace_key=ws_keys[0] if ws_keys else None,
            )
            field_hlcs = {
                k: v
                for k, v in _load_field_hlcs(r["field_hlcs_json"]).items()
                if v and v != _UNKNOWN_FIELD_HLC and not k.startswith("_")
            }
            if self.append_entity_change(
                hlc=_row_hlc(r, hlc),
                device_id=device_id,
                entity="user",
                entity_key=user.user_id,
                payload={
                    "user_id": user.user_id,
                    "team_id": user.team_id,
                    "name": user.name,
                    "real_name": user.real_name,
                    "display_name": user.display_name,
                    "title": user.title,
                    "email": None,
                    "is_bot": user.is_bot,
                    "is_deleted": user.is_deleted,
                    "is_restricted": user.is_restricted,
                    "is_ultra_restricted": user.is_ultra_restricted,
                    "is_external": user.is_external,
                    "tz": user.tz,
                    "raw": user.raw,
                    "workspace_key": user.workspace_key,
                    "workspace_keys": ws_keys,
                    "first_seen": r["first_seen"],
                    "last_seen": r["last_seen"],
                    "field_hlcs": field_hlcs,
                    "clock_epoch": self.directory_clock_epoch(),
                    **({"clock_repair": True} if emit_clock_repair else {}),
                },
            ):
                counts["users"] += 1
        for r in self._conn.execute("SELECT * FROM message_versions").fetchall():
            entity_key = f"{r['channel_id']}|{r['ts']}|{r['content_sha256']}"
            if self._has_entity_outbox(device_id, "message_version", entity_key):
                continue
            ch = self._conn.execute(
                "SELECT workspace_key FROM channels WHERE channel_id=?",
                (r["channel_id"],),
            ).fetchone()
            workspace_key = ch["workspace_key"] if ch else "unknown"
            raw = json.loads(r["raw_json"] or "{}")
            payload = {
                "channel_id": r["channel_id"],
                "ts": r["ts"],
                "workspace_key": workspace_key,
                "content_sha256": r["content_sha256"],
                "edited_ts": r["edited_ts"],
                "is_deletion": bool(r["is_deletion"]),
                "is_partial": bool(r["is_partial"]),
                "source_rank": r["source_rank"],
                "user_id": r["user_id"],
                "thread_ts": r["thread_ts"],
                "subtype": r["subtype"],
                "text": r["text"],
                "search_text": r["search_text"],
                "raw": raw,
                "source": "backfill",
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"],
            }
            if self.append_entity_change(
                hlc=hlc,
                device_id=device_id,
                entity="message_version",
                entity_key=entity_key,
                payload=payload,
            ):
                counts["message_versions"] += 1
        for r in self._conn.execute(
            """
            SELECT * FROM message_sightings
            WHERE device_id=?
            """,
            (device_id,),
        ).fetchall():
            sighting_key = (
                f"{r['channel_id']}|{r['ts']}|{r['content_sha256']}|"
                f"{r['device_id']}|{r['source']}"
            )
            if self._has_entity_outbox(device_id, "sighting", sighting_key):
                continue
            if self.append_entity_change(
                hlc=hlc,
                device_id=device_id,
                entity="sighting",
                entity_key=sighting_key,
                payload={
                    "channel_id": r["channel_id"],
                    "ts": r["ts"],
                    "content_sha256": r["content_sha256"],
                    "device_id": r["device_id"],
                    "source": r["source"],
                    "first_seen": r["first_seen"],
                    "last_seen": r["last_seen"],
                    "snapshot_id": r["last_snapshot_id"] or r["first_snapshot_id"] or "backfill",
                    "state_sha256": r["last_state_sha256"] or "backfill",
                },
            ):
                counts["sightings"] += 1
        if emit_clock_repair:
            self.set_meta("field_hlcs_policy", "v8-epoch-repair", commit=True)
        return counts

    def list_change_log_since(
        self, *, device_id: str, since_seq: int
    ) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT seq, hlc, device_id, entity, entity_key, op, payload_json, payload_sha256
            FROM change_log
            WHERE device_id=? AND seq > ?
            ORDER BY seq ASC
            """,
            (device_id, since_seq),
        ).fetchall()
        return [dict(r) for r in rows]

    def append_entity_change(
        self,
        *,
        hlc: str,
        device_id: str,
        entity: str,
        entity_key: str,
        payload: dict[str, Any],
        op: str = "upsert",
    ) -> bool:
        from slack_cache_mcp.desktop.sanitize import sanitize

        clean = sanitize(payload)
        if not isinstance(clean, dict):
            raise TypeError("change_log payload must sanitize to a dict")
        blob = json.dumps(clean, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        sha = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        return self.insert_change_log_row(
            hlc=hlc,
            device_id=device_id,
            entity=entity,
            entity_key=entity_key,
            op=op,
            payload=clean,
            payload_sha256=sha,
        )

    def upsert_source(
        self,
        source_id: str,
        device_id: str,
        kind: str,
        path_hint: str,
        slack_version: str | None,
    ) -> None:
        now = _utc_now()
        self._conn.execute(
            """
            INSERT INTO sources(source_id, device_id, kind, path_hint, slack_version, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
              last_seen=excluded.last_seen,
              slack_version=COALESCE(excluded.slack_version, sources.slack_version)
            """,
            (source_id, device_id, kind, path_hint, slack_version, now, now),
        )
        self._conn.commit()

    def begin_snapshot(self, snapshot_id: str, source_id: str, trigger: str) -> None:
        self._conn.execute(
            """
            INSERT INTO snapshots(snapshot_id, source_id, started_at, trigger, stats_json)
            VALUES (?, ?, ?, ?, '{}')
            """,
            (snapshot_id, source_id, _utc_now(), trigger),
        )
        self._conn.commit()

    def finish_snapshot(
        self,
        snapshot_id: str,
        *,
        files_copied: int,
        bytes_copied: int,
        states_decoded: int,
        decode_errors: int,
        stats_json: str,
    ) -> None:
        self._conn.execute(
            """
            UPDATE snapshots SET finished_at=?, files_copied=?, bytes_copied=?,
              states_decoded=?, decode_errors=?, stats_json=?
            WHERE snapshot_id=?
            """,
            (
                _utc_now(),
                files_copied,
                bytes_copied,
                states_decoded,
                decode_errors,
                stats_json,
                snapshot_id,
            ),
        )
        self._conn.commit()

    def has_state_generation(self, state_sha256: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM state_generations WHERE state_sha256=?",
            (state_sha256,),
        ).fetchone()
        return row is not None

    def touch_state_generation(self, state_sha256: str, seen_at: str) -> None:
        self._conn.execute(
            "UPDATE state_generations SET last_seen=? WHERE state_sha256=?",
            (seen_at, state_sha256),
        )
        self._conn.commit()

    def insert_state_generation(
        self,
        state_sha256: str,
        workspace_key: str,
        self_user_id: str | None,
        blob_ref: str | None,
        snapshot_id: str,
        seen_at: str,
        *,
        vault_path: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO state_generations(
              state_sha256, workspace_key, self_user_id, blob_ref,
              first_seen, last_seen, first_snapshot_id, vault_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state_sha256,
                workspace_key,
                self_user_id,
                blob_ref,
                seen_at,
                seen_at,
                snapshot_id,
                vault_path,
            ),
        )
        self._conn.commit()

    def set_state_vault_path(self, state_sha256: str, vault_path: str) -> None:
        self._conn.execute(
            "UPDATE state_generations SET vault_path=? WHERE state_sha256=?",
            (vault_path, state_sha256),
        )
        self._conn.commit()

    def upsert_coverage_range(
        self,
        channel_id: str,
        start_ts: str,
        end_ts: str,
        *,
        contiguous_to_latest: bool,
        seen_at: str,
    ) -> None:
        existing = self._conn.execute(
            "SELECT start_ts, end_ts FROM coverage_ranges WHERE channel_id=?",
            (channel_id,),
        ).fetchall()
        intervals = [(r["start_ts"], r["end_ts"]) for r in existing]
        from slack_cache_mcp.ingest.coverage import merge_coverage_interval

        merged = merge_coverage_interval(intervals, start_ts, end_ts)
        self._conn.execute("DELETE FROM coverage_ranges WHERE channel_id=?", (channel_id,))
        for s, e in merged:
            cont = 1 if contiguous_to_latest and s == merged[-1][0] and e == merged[-1][1] else 0
            self._conn.execute(
                """
                INSERT INTO coverage_ranges(
                  channel_id, start_ts, end_ts, contiguous_to_latest, first_seen, last_seen
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (channel_id, s, e, cont, seen_at, seen_at),
            )
        self._conn.commit()

    def update_channel_status(
        self,
        channel_id: str,
        *,
        newest_ts: str | None,
        oldest_ts: str | None,
        captured_count: int,
        latest_ts_reported: str | None = None,
        unread_count: int | None = None,
        seen_at: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO channel_status(
              channel_id, latest_ts_reported, newest_captured_ts, oldest_captured_ts,
              captured_count, unread_count, last_window_seen, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
              latest_ts_reported=COALESCE(excluded.latest_ts_reported, channel_status.latest_ts_reported),
              newest_captured_ts=excluded.newest_captured_ts,
              oldest_captured_ts=excluded.oldest_captured_ts,
              captured_count=excluded.captured_count,
              unread_count=COALESCE(excluded.unread_count, channel_status.unread_count),
              last_window_seen=excluded.last_window_seen,
              updated_at=excluded.updated_at
            """,
            (
                channel_id,
                latest_ts_reported,
                newest_ts,
                oldest_ts,
                captured_count,
                unread_count,
                seen_at,
                seen_at,
            ),
        )
        self._conn.commit()

    def count_messages(self, channel_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE channel_id=?",
            (channel_id,),
        ).fetchone()
        return int(row["c"]) if row else 0

    def list_edge_sighted_ts(self, channel_id: str, candidates: set[str]) -> set[str]:
        """Return subset of ``candidates`` with a *live* ``edge.*`` sighting.

        Excludes historical/deleted LevelDB provenance (``%historical%``) so stale
        Edge tombstones cannot block desktop suspected-deletion.
        """
        if not candidates:
            return set()
        out: set[str] = set()
        # Chunk IN lists for SQLite variable limits.
        items = list(candidates)
        for i in range(0, len(items), 400):
            chunk = items[i : i + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = self._conn.execute(
                f"""
                SELECT DISTINCT ts FROM message_sightings
                WHERE channel_id=?
                  AND source LIKE 'edge.%'
                  AND source NOT LIKE '%historical%'
                  AND ts IN ({placeholders})
                """,
                (channel_id, *chunk),
            ).fetchall()
            out.update(str(r["ts"]) for r in rows)
        return out

    def list_known_message_ts(
        self,
        channel_id: str,
        *,
        start_ts: str | None = None,
        end_ts: str | None = None,
    ) -> set[str]:
        """Return known message ts for a channel, optionally constrained to a window."""
        sql = "SELECT ts FROM messages WHERE channel_id=?"
        params: list[Any] = [channel_id]
        if start_ts is not None:
            sql += " AND ts_num >= ?"
            params.append(_ts_num(start_ts))
        if end_ts is not None:
            sql += " AND ts_num <= ?"
            params.append(_ts_num(end_ts))
        rows = self._conn.execute(sql, params).fetchall()
        return {r["ts"] for r in rows}

    def apply_suspected_deletions(
        self,
        channel_id: str,
        missing_ts: set[str],
        *,
        state_sha256: str,
        seen_at: str,
        threshold: int = 2,
    ) -> list[str]:
        """Increment streaks for missing ts; mark suspected when streak >= threshold.

        Returns list of ts newly marked suspected. Messages already ``deleted`` stay deleted.
        """
        marked: list[str] = []
        # Clear streaks for messages present again (handled by caller passing only missing)
        # Reset streaks for ts not in missing: done below via delete of others for this channel
        # that were not missing this generation.
        existing = self._conn.execute(
            "SELECT ts, streak FROM suspected_streaks WHERE channel_id=?",
            (channel_id,),
        ).fetchall()
        existing_map = {r["ts"]: r["streak"] for r in existing}

        # Drop streaks for ts that reappeared
        for ts in list(existing_map):
            if ts not in missing_ts:
                self._conn.execute(
                    "DELETE FROM suspected_streaks WHERE channel_id=? AND ts=?",
                    (channel_id, ts),
                )
                row = self._conn.execute(
                    "SELECT deleted_state FROM messages WHERE channel_id=? AND ts=?",
                    (channel_id, ts),
                ).fetchone()
                if row and row["deleted_state"] == "suspected":
                    self._conn.execute(
                        "UPDATE messages SET deleted_state='none' WHERE channel_id=? AND ts=?",
                        (channel_id, ts),
                    )

        for ts in missing_ts:
            streak = int(existing_map.get(ts, 0)) + 1
            self._conn.execute(
                """
                INSERT INTO suspected_streaks(channel_id, ts, streak, last_state_sha256, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(channel_id, ts) DO UPDATE SET
                  streak=excluded.streak,
                  last_state_sha256=excluded.last_state_sha256,
                  last_seen=excluded.last_seen
                """,
                (channel_id, ts, streak, state_sha256, seen_at),
            )
            if streak >= threshold:
                row = self._conn.execute(
                    "SELECT deleted_state, thread_ts FROM messages WHERE channel_id=? AND ts=?",
                    (channel_id, ts),
                ).fetchone()
                if row and row["deleted_state"] == "none":
                    self._conn.execute(
                        "UPDATE messages SET deleted_state='suspected' WHERE channel_id=? AND ts=?",
                        (channel_id, ts),
                    )
                    marked.append(ts)
                    # Drop message docs / refresh aggregates so deleted text
                    # cannot linger in thread/window ``doc_text``.
                    try:
                        from slack_cache_mcp.search.docs import SearchDocsBuilder

                        builder = SearchDocsBuilder(self)
                        builder.drop_message_doc(channel_id, ts)
                        thread_root = row["thread_ts"] or ts
                        builder.refresh_aggregates(
                            channel_id,
                            thread_root,
                            message_ts=ts,
                            previous_thread_ts=row["thread_ts"],
                        )
                        # Root deletion must clear parent previews on reply docs.
                        if thread_root == ts:
                            builder.refresh_reply_docs_after_parent_change(
                                channel_id, ts
                            )
                    except Exception:
                        pass
        self._conn.commit()
        return marked

    def upsert_workspace(
        self,
        ws: Workspace,
        hlc: str,
        seen_at: str,
        *,
        device_id: str | None = None,
        write_change_log: bool = True,
        hlc_lww: bool = True,
        commit: bool = True,
        incoming_field_hlcs: dict[str, str] | None = None,
        force_fields: bool = False,
        fill_nulls_only: bool = False,
    ) -> None:
        raw = json.dumps(ws.raw, ensure_ascii=False)
        _ = hlc_lww  # always maintain per-field clocks
        existing = self._conn.execute(
            "SELECT * FROM workspaces WHERE workspace_key=?",
            (ws.workspace_key,),
        ).fetchone()
        if existing is None:
            field_hlcs: dict[str, str] = {}
            for key, val in (
                ("team_id", ws.team_id),
                ("enterprise_id", ws.enterprise_id),
                ("name", ws.name),
                ("domain", ws.domain),
                ("self_user_id", ws.self_user_id),
            ):
                if val is None:
                    continue
                if incoming_field_hlcs is not None:
                    field_hlcs[key] = incoming_field_hlcs.get(key, _UNKNOWN_FIELD_HLC)
                else:
                    field_hlcs[key] = hlc
            if incoming_field_hlcs and incoming_field_hlcs.get(_REPAIR_HLC_KEY):
                field_hlcs[_REPAIR_HLC_KEY] = incoming_field_hlcs[_REPAIR_HLC_KEY]
            self._conn.execute(
                """
                INSERT INTO workspaces(
                  workspace_key, team_id, enterprise_id, name, domain, self_user_id,
                  self_is_guest, raw_json, first_seen, last_seen, updated_hlc,
                  field_hlcs_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ws.workspace_key,
                    ws.team_id,
                    ws.enterprise_id,
                    ws.name,
                    ws.domain,
                    ws.self_user_id,
                    1 if ws.self_is_guest else 0,
                    raw,
                    seen_at,
                    seen_at,
                    hlc,
                    _dump_field_hlcs(field_hlcs),
                ),
            )
        else:
            field_hlcs = _load_field_hlcs(existing["field_hlcs_json"])

            def _take(field: str, current: Any, incoming: Any) -> Any:
                if fill_nulls_only:
                    if current is not None or incoming is None:
                        return current
                    field_hlcs[field] = _UNKNOWN_FIELD_HLC
                    return incoming
                if force_fields:
                    if incoming is None:
                        return current
                    forced = (
                        incoming_field_hlcs.get(field)
                        if incoming_field_hlcs is not None
                        else None
                    )
                    field_hlcs[field] = forced if forced else _UNKNOWN_FIELD_HLC
                    return incoming
                if incoming_field_hlcs is not None:
                    clock_in: str | None = incoming_field_hlcs.get(field)
                    replace_unknown = False
                else:
                    clock_in = hlc
                    replace_unknown = True
                value, clock = _merge_sparse_field(
                    current,
                    field_hlcs.get(field),
                    incoming,
                    clock_in,
                    replace_unknown=replace_unknown,
                )
                if clock:
                    field_hlcs[field] = clock
                else:
                    field_hlcs.pop(field, None)
                return value

            team_id = _take("team_id", existing["team_id"], ws.team_id)
            enterprise_id = _take(
                "enterprise_id", existing["enterprise_id"], ws.enterprise_id
            )
            name = _take("name", existing["name"], ws.name)
            domain = _take("domain", existing["domain"], ws.domain)
            self_user_id = _take(
                "self_user_id", existing["self_user_id"], ws.self_user_id
            )
            if force_fields and incoming_field_hlcs:
                repair_hlc = incoming_field_hlcs.get(_REPAIR_HLC_KEY)
                if repair_hlc:
                    field_hlcs[_REPAIR_HLC_KEY] = repair_hlc
            row_wins = _hlc_dominates(hlc, existing["updated_hlc"])
            self_is_guest = (
                (1 if ws.self_is_guest else 0) if row_wins else existing["self_is_guest"]
            )
            raw_json = raw if row_wins else existing["raw_json"]
            updated_hlc = hlc if row_wins else existing["updated_hlc"]
            self._conn.execute(
                """
                UPDATE workspaces SET
                  team_id=?, enterprise_id=?, name=?, domain=?, self_user_id=?,
                  self_is_guest=?, raw_json=?, last_seen=MAX(last_seen, ?),
                  updated_hlc=?, field_hlcs_json=?
                WHERE workspace_key=?
                """,
                (
                    team_id,
                    enterprise_id,
                    name,
                    domain,
                    self_user_id,
                    self_is_guest,
                    raw_json,
                    seen_at,
                    updated_hlc,
                    _dump_field_hlcs(field_hlcs),
                    ws.workspace_key,
                ),
            )
        if write_change_log and device_id:
            # Emit the merged stored row + its field clocks (not the possibly-losing
            # incoming observation), so peers cannot accept a rejected value@winning clock.
            stored = self._conn.execute(
                "SELECT * FROM workspaces WHERE workspace_key=?",
                (ws.workspace_key,),
            ).fetchone()
            if stored is not None:
                self.append_entity_change(
                    hlc=str(stored["updated_hlc"] or hlc),
                    device_id=device_id,
                    entity="workspace",
                    entity_key=ws.workspace_key,
                    payload={
                        "workspace_key": stored["workspace_key"],
                        "team_id": stored["team_id"],
                        "enterprise_id": stored["enterprise_id"],
                        "name": stored["name"],
                        "domain": stored["domain"],
                        "self_user_id": stored["self_user_id"],
                        "self_is_guest": bool(stored["self_is_guest"]),
                        "raw": json.loads(stored["raw_json"] or "{}"),
                        "first_seen": stored["first_seen"],
                        "last_seen": stored["last_seen"],
                            "field_hlcs": {
                                k: v
                                for k, v in _load_field_hlcs(stored["field_hlcs_json"]).items()
                                if v and v != _UNKNOWN_FIELD_HLC and not k.startswith("_")
                            },
                            "clock_epoch": self.directory_clock_epoch(),
                        },
                    )
        if commit:
            self._conn.commit()

    def upsert_channel(
        self,
        ch: Channel,
        hlc: str,
        seen_at: str,
        *,
        device_id: str | None = None,
        write_change_log: bool = True,
        hlc_lww: bool = True,
        commit: bool = True,
        incoming_field_hlcs: dict[str, str] | None = None,
        force_fields: bool = False,
        fill_nulls_only: bool = False,
    ) -> None:
        raw = json.dumps(ch.raw, ensure_ascii=False)
        if hlc_lww:
            existing = self._conn.execute(
                "SELECT * FROM channels WHERE channel_id=?",
                (ch.channel_id,),
            ).fetchone()
            if existing is None:
                if incoming_field_hlcs is not None:
                    field_hlcs = {
                        "workspace_key": incoming_field_hlcs.get(
                            "workspace_key", _UNKNOWN_FIELD_HLC
                        )
                    }
                else:
                    field_hlcs = {"workspace_key": hlc}
                for key, val in (
                    ("name", ch.name),
                    ("topic", ch.topic),
                    ("purpose", ch.purpose),
                    ("im_user_id", ch.im_user_id),
                    ("context_team_id", ch.context_team_id),
                ):
                    if val is None:
                        continue
                    if incoming_field_hlcs is not None:
                        field_hlcs[key] = incoming_field_hlcs.get(key, _UNKNOWN_FIELD_HLC)
                    else:
                        field_hlcs[key] = hlc
                if incoming_field_hlcs and incoming_field_hlcs.get(_REPAIR_HLC_KEY):
                    field_hlcs[_REPAIR_HLC_KEY] = incoming_field_hlcs[_REPAIR_HLC_KEY]
                self._conn.execute(
                    """
                    INSERT INTO channels(
                      channel_id, workspace_key, context_team_id, name, kind,
                      is_ext_shared, is_archived, is_member, topic, purpose, im_user_id,
                      raw_json, first_seen, last_seen, updated_hlc, field_hlcs_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ch.channel_id,
                        ch.workspace_key,
                        ch.context_team_id,
                        ch.name,
                        ch.kind,
                        1 if ch.is_ext_shared else 0,
                        1 if ch.is_archived else 0,
                        1 if ch.is_member else 0,
                        ch.topic,
                        ch.purpose,
                        ch.im_user_id,
                        raw,
                        seen_at,
                        seen_at,
                        hlc,
                        _dump_field_hlcs(field_hlcs),
                    ),
                )
            else:
                field_hlcs = _load_field_hlcs(existing["field_hlcs_json"])

                def _take(field: str, current: Any, incoming: Any) -> Any:
                    if fill_nulls_only:
                        if current is not None or incoming is None:
                            return current
                        field_hlcs[field] = _UNKNOWN_FIELD_HLC
                        return incoming
                    if force_fields:
                        if incoming is None:
                            return current
                        forced = (
                            incoming_field_hlcs.get(field)
                            if incoming_field_hlcs is not None
                            else None
                        )
                        field_hlcs[field] = forced if forced else _UNKNOWN_FIELD_HLC
                        return incoming
                    if incoming_field_hlcs is not None:
                        clock_in: str | None = incoming_field_hlcs.get(field)
                        replace_unknown = False
                    else:
                        clock_in = hlc
                        replace_unknown = True
                    value, clock = _merge_sparse_field(
                        current,
                        field_hlcs.get(field),
                        incoming,
                        clock_in,
                        replace_unknown=replace_unknown,
                    )
                    if clock:
                        field_hlcs[field] = clock
                    else:
                        field_hlcs.pop(field, None)
                    return value

                # workspace_key is required; still LWW by its own field clock so
                # opposite delivery orders converge.
                workspace_key = _take(
                    "workspace_key", existing["workspace_key"], ch.workspace_key
                )
                name = _take("name", existing["name"], ch.name)
                topic = _take("topic", existing["topic"], ch.topic)
                purpose = _take("purpose", existing["purpose"], ch.purpose)
                im_user_id = _take("im_user_id", existing["im_user_id"], ch.im_user_id)
                context_team_id = _take(
                    "context_team_id", existing["context_team_id"], ch.context_team_id
                )
                if force_fields and incoming_field_hlcs:
                    repair_hlc = incoming_field_hlcs.get(_REPAIR_HLC_KEY)
                    if repair_hlc:
                        field_hlcs[_REPAIR_HLC_KEY] = repair_hlc
                row_wins = _hlc_dominates(hlc, existing["updated_hlc"])
                kind = ch.kind if row_wins else existing["kind"]
                is_ext_shared = (
                    (1 if ch.is_ext_shared else 0) if row_wins else existing["is_ext_shared"]
                )
                is_archived = (
                    (1 if ch.is_archived else 0) if row_wins else existing["is_archived"]
                )
                is_member = (1 if ch.is_member else 0) if row_wins else existing["is_member"]
                raw_json = raw if row_wins else existing["raw_json"]
                updated_hlc = hlc if row_wins else existing["updated_hlc"]
                self._conn.execute(
                    """
                    UPDATE channels SET
                      workspace_key=?, context_team_id=?, name=?, kind=?,
                      is_ext_shared=?, is_archived=?, is_member=?,
                      topic=?, purpose=?, im_user_id=?, raw_json=?,
                      last_seen=MAX(last_seen, ?), updated_hlc=?, field_hlcs_json=?
                    WHERE channel_id=?
                    """,
                    (
                        workspace_key,
                        context_team_id,
                        name,
                        kind,
                        is_ext_shared,
                        is_archived,
                        is_member,
                        topic,
                        purpose,
                        im_user_id,
                        raw_json,
                        seen_at,
                        updated_hlc,
                        _dump_field_hlcs(field_hlcs),
                        ch.channel_id,
                    ),
                )
            self._conn.execute(
                """
                INSERT OR IGNORE INTO channel_workspaces(channel_id, workspace_key)
                VALUES (?, ?)
                """,
                (ch.channel_id, ch.workspace_key),
            )
            if ch.name:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO channel_aliases(channel_id, name, first_seen)
                    VALUES (?, ?, ?)
                    """,
                    (ch.channel_id, ch.name, seen_at),
                )
            if write_change_log and device_id:
                stored = self._conn.execute(
                    "SELECT * FROM channels WHERE channel_id=?",
                    (ch.channel_id,),
                ).fetchone()
                if stored is not None:
                    ws_keys = [
                        row["workspace_key"]
                        for row in self._conn.execute(
                            "SELECT workspace_key FROM channel_workspaces WHERE channel_id=?",
                            (ch.channel_id,),
                        ).fetchall()
                    ]
                    aliases = [
                        {"name": row["name"], "first_seen": row["first_seen"]}
                        for row in self._conn.execute(
                            "SELECT name, first_seen FROM channel_aliases WHERE channel_id=?",
                            (ch.channel_id,),
                        ).fetchall()
                    ]
                    self.append_entity_change(
                        hlc=str(stored["updated_hlc"] or hlc),
                        device_id=device_id,
                        entity="channel",
                        entity_key=ch.channel_id,
                        payload={
                            "channel_id": stored["channel_id"],
                            "workspace_key": stored["workspace_key"],
                            "workspace_keys": ws_keys,
                            "aliases": [a["name"] for a in aliases],
                            "alias_records": aliases,
                            "context_team_id": stored["context_team_id"],
                            "name": stored["name"],
                            "kind": stored["kind"],
                            "is_ext_shared": bool(stored["is_ext_shared"]),
                            "is_archived": bool(stored["is_archived"]),
                            "is_member": bool(stored["is_member"]),
                            "topic": stored["topic"],
                            "purpose": stored["purpose"],
                            "im_user_id": stored["im_user_id"],
                            "raw": json.loads(stored["raw_json"] or "{}"),
                            "first_seen": stored["first_seen"],
                            "last_seen": stored["last_seen"],
                            "field_hlcs": {
                                k: v
                                for k, v in _load_field_hlcs(
                                    stored["field_hlcs_json"]
                                ).items()
                                if v and v != _UNKNOWN_FIELD_HLC and not k.startswith("_")
                            },
                            "clock_epoch": self.directory_clock_epoch(),
                        },
                    )
            if commit:
                self._conn.commit()
            return
        self._conn.execute(
            """
            INSERT INTO channels(
              channel_id, workspace_key, context_team_id, name, kind,
              is_ext_shared, is_archived, is_member, topic, purpose, im_user_id,
              raw_json, first_seen, last_seen, updated_hlc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
              workspace_key=excluded.workspace_key,
              name=COALESCE(excluded.name, channels.name),
              kind=excluded.kind,
              is_ext_shared=excluded.is_ext_shared,
              is_archived=excluded.is_archived,
              is_member=excluded.is_member,
              topic=COALESCE(excluded.topic, channels.topic),
              purpose=COALESCE(excluded.purpose, channels.purpose),
              raw_json=excluded.raw_json,
              last_seen=excluded.last_seen,
              updated_hlc=excluded.updated_hlc
            """,
            (
                ch.channel_id,
                ch.workspace_key,
                ch.context_team_id,
                ch.name,
                ch.kind,
                1 if ch.is_ext_shared else 0,
                1 if ch.is_archived else 0,
                1 if ch.is_member else 0,
                ch.topic,
                ch.purpose,
                ch.im_user_id,
                raw,
                seen_at,
                seen_at,
                hlc,
            ),
        )
        self._conn.execute(
            """
            INSERT OR IGNORE INTO channel_workspaces(channel_id, workspace_key)
            VALUES (?, ?)
            """,
            (ch.channel_id, ch.workspace_key),
        )
        if ch.name:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO channel_aliases(channel_id, name, first_seen)
                VALUES (?, ?, ?)
                """,
                (ch.channel_id, ch.name, seen_at),
            )
        if write_change_log and device_id:
            self.append_entity_change(
                hlc=hlc,
                device_id=device_id,
                entity="channel",
                entity_key=ch.channel_id,
                payload={
                    "channel_id": ch.channel_id,
                    "workspace_key": ch.workspace_key,
                    "context_team_id": ch.context_team_id,
                    "name": ch.name,
                    "kind": ch.kind,
                    "is_ext_shared": ch.is_ext_shared,
                    "is_archived": ch.is_archived,
                    "is_member": ch.is_member,
                    "topic": ch.topic,
                    "purpose": ch.purpose,
                    "im_user_id": ch.im_user_id,
                    "raw": ch.raw,
                    "first_seen": seen_at,
                    "last_seen": seen_at,
                },
            )
        if commit:
            self._conn.commit()

    def upsert_user(
        self,
        user: User,
        hlc: str,
        seen_at: str,
        *,
        device_id: str | None = None,
        write_change_log: bool = True,
        hlc_lww: bool = True,
        commit: bool = True,
        workspace_keys: list[str] | None = None,
        incoming_field_hlcs: dict[str, str] | None = None,
        force_fields: bool = False,
        fill_nulls_only: bool = False,
    ) -> None:
        raw = json.dumps(user.raw, ensure_ascii=False)
        assoc_keys = list(workspace_keys or [])
        if user.workspace_key and user.workspace_key not in assoc_keys:
            assoc_keys.append(user.workspace_key)
        if hlc_lww:
            existing = self._conn.execute(
                "SELECT * FROM users WHERE user_id=?",
                (user.user_id,),
            ).fetchone()
            if existing is None:
                field_hlcs = {}
                for key, val in (
                    ("name", user.name),
                    ("real_name", user.real_name),
                    ("display_name", user.display_name),
                    ("title", user.title),
                    ("tz", user.tz),
                    ("team_id", user.team_id),
                ):
                    if val is None:
                        continue
                    if incoming_field_hlcs is not None:
                        field_hlcs[key] = incoming_field_hlcs.get(key, _UNKNOWN_FIELD_HLC)
                    else:
                        field_hlcs[key] = hlc
                if incoming_field_hlcs and incoming_field_hlcs.get(_REPAIR_HLC_KEY):
                    field_hlcs[_REPAIR_HLC_KEY] = incoming_field_hlcs[_REPAIR_HLC_KEY]
                self._conn.execute(
                    """
                    INSERT INTO users(
                      user_id, team_id, name, real_name, display_name, title, email,
                      is_bot, is_deleted, is_restricted, is_ultra_restricted, is_external,
                      tz, raw_json, first_seen, last_seen, updated_hlc, field_hlcs_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user.user_id,
                        user.team_id,
                        user.name,
                        user.real_name,
                        user.display_name,
                        user.title,
                        None,
                        1 if user.is_bot else 0,
                        1 if user.is_deleted else 0,
                        1 if user.is_restricted else 0,
                        1 if user.is_ultra_restricted else 0,
                        1 if user.is_external else 0,
                        user.tz,
                        raw,
                        seen_at,
                        seen_at,
                        hlc,
                        _dump_field_hlcs(field_hlcs),
                    ),
                )
            else:
                field_hlcs = _load_field_hlcs(existing["field_hlcs_json"])

                def _take(field: str, current: Any, incoming: Any) -> Any:
                    if fill_nulls_only:
                        if current is not None or incoming is None:
                            return current
                        field_hlcs[field] = _UNKNOWN_FIELD_HLC
                        return incoming
                    if force_fields:
                        if incoming is None:
                            return current
                        forced = (
                            incoming_field_hlcs.get(field)
                            if incoming_field_hlcs is not None
                            else None
                        )
                        field_hlcs[field] = forced if forced else _UNKNOWN_FIELD_HLC
                        return incoming
                    if incoming_field_hlcs is not None:
                        clock_in: str | None = incoming_field_hlcs.get(field)
                        replace_unknown = False
                    else:
                        clock_in = hlc
                        replace_unknown = True
                    value, clock = _merge_sparse_field(
                        current,
                        field_hlcs.get(field),
                        incoming,
                        clock_in,
                        replace_unknown=replace_unknown,
                    )
                    if clock:
                        field_hlcs[field] = clock
                    else:
                        field_hlcs.pop(field, None)
                    return value

                name = _take("name", existing["name"], user.name)
                real_name = _take("real_name", existing["real_name"], user.real_name)
                display_name = _take(
                    "display_name", existing["display_name"], user.display_name
                )
                title = _take("title", existing["title"], user.title)
                tz = _take("tz", existing["tz"], user.tz)
                team_id = _take("team_id", existing["team_id"], user.team_id)
                if force_fields and incoming_field_hlcs:
                    repair_hlc = incoming_field_hlcs.get(_REPAIR_HLC_KEY)
                    if repair_hlc:
                        field_hlcs[_REPAIR_HLC_KEY] = repair_hlc
                row_wins = _hlc_dominates(hlc, existing["updated_hlc"])
                flags = {
                    "is_bot": (1 if user.is_bot else 0) if row_wins else existing["is_bot"],
                    "is_deleted": (
                        (1 if user.is_deleted else 0) if row_wins else existing["is_deleted"]
                    ),
                    "is_restricted": (
                        (1 if user.is_restricted else 0)
                        if row_wins
                        else existing["is_restricted"]
                    ),
                    "is_ultra_restricted": (
                        (1 if user.is_ultra_restricted else 0)
                        if row_wins
                        else existing["is_ultra_restricted"]
                    ),
                    "is_external": (
                        (1 if user.is_external else 0) if row_wins else existing["is_external"]
                    ),
                }
                raw_json = raw if row_wins else existing["raw_json"]
                updated_hlc = hlc if row_wins else existing["updated_hlc"]
                self._conn.execute(
                    """
                    UPDATE users SET
                      team_id=?, name=?, real_name=?, display_name=?, title=?,
                      is_bot=?, is_deleted=?, is_restricted=?, is_ultra_restricted=?,
                      is_external=?, tz=?, raw_json=?, last_seen=MAX(last_seen, ?),
                      updated_hlc=?, field_hlcs_json=?
                    WHERE user_id=?
                    """,
                    (
                        team_id,
                        name,
                        real_name,
                        display_name,
                        title,
                        flags["is_bot"],
                        flags["is_deleted"],
                        flags["is_restricted"],
                        flags["is_ultra_restricted"],
                        flags["is_external"],
                        tz,
                        raw_json,
                        seen_at,
                        updated_hlc,
                        _dump_field_hlcs(field_hlcs),
                        user.user_id,
                    ),
                )
            for wk in assoc_keys:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO user_workspaces(user_id, workspace_key)
                    VALUES (?, ?)
                    """,
                    (user.user_id, wk),
                )
            if write_change_log and device_id:
                stored = self._conn.execute(
                    "SELECT * FROM users WHERE user_id=?",
                    (user.user_id,),
                ).fetchone()
                ws_stored = [
                    row["workspace_key"]
                    for row in self._conn.execute(
                        "SELECT workspace_key FROM user_workspaces WHERE user_id=?",
                        (user.user_id,),
                    ).fetchall()
                ]
                if stored is not None:
                    self.append_entity_change(
                        hlc=str(stored["updated_hlc"] or hlc),
                        device_id=device_id,
                        entity="user",
                        entity_key=user.user_id,
                        payload={
                            "user_id": stored["user_id"],
                            "team_id": stored["team_id"],
                            "name": stored["name"],
                            "real_name": stored["real_name"],
                            "display_name": stored["display_name"],
                            "title": stored["title"],
                            "email": None,
                            "is_bot": bool(stored["is_bot"]),
                            "is_deleted": bool(stored["is_deleted"]),
                            "is_restricted": bool(stored["is_restricted"]),
                            "is_ultra_restricted": bool(stored["is_ultra_restricted"]),
                            "is_external": bool(stored["is_external"]),
                            "tz": stored["tz"],
                            "raw": json.loads(stored["raw_json"] or "{}"),
                            "workspace_key": ws_stored[0] if ws_stored else None,
                            "workspace_keys": ws_stored,
                            "first_seen": stored["first_seen"],
                            "last_seen": stored["last_seen"],
                            "field_hlcs": {
                                k: v
                                for k, v in _load_field_hlcs(
                                    stored["field_hlcs_json"]
                                ).items()
                                if v and v != _UNKNOWN_FIELD_HLC and not k.startswith("_")
                            },
                            "clock_epoch": self.directory_clock_epoch(),
                        },
                    )
            if commit:
                self._conn.commit()
            return
        self._conn.execute(
            """
            INSERT INTO users(
              user_id, team_id, name, real_name, display_name, title, email,
              is_bot, is_deleted, is_restricted, is_ultra_restricted, is_external,
              tz, raw_json, first_seen, last_seen, updated_hlc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
              name=COALESCE(excluded.name, users.name),
              real_name=COALESCE(excluded.real_name, users.real_name),
              display_name=COALESCE(excluded.display_name, users.display_name),
              title=COALESCE(excluded.title, users.title),
              is_bot=excluded.is_bot,
              is_deleted=excluded.is_deleted,
              is_restricted=excluded.is_restricted,
              is_ultra_restricted=excluded.is_ultra_restricted,
              is_external=excluded.is_external,
              raw_json=excluded.raw_json,
              last_seen=excluded.last_seen,
              updated_hlc=excluded.updated_hlc
            """,
            (
                user.user_id,
                user.team_id,
                user.name,
                user.real_name,
                user.display_name,
                user.title,
                None,
                1 if user.is_bot else 0,
                1 if user.is_deleted else 0,
                1 if user.is_restricted else 0,
                1 if user.is_ultra_restricted else 0,
                1 if user.is_external else 0,
                user.tz,
                raw,
                seen_at,
                seen_at,
                hlc,
            ),
        )
        if user.workspace_key:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO user_workspaces(user_id, workspace_key)
                VALUES (?, ?)
                """,
                (user.user_id, user.workspace_key),
            )
        if write_change_log and device_id:
            self.append_entity_change(
                hlc=hlc,
                device_id=device_id,
                entity="user",
                entity_key=user.user_id,
                payload={
                    "user_id": user.user_id,
                    "team_id": user.team_id,
                    "name": user.name,
                    "real_name": user.real_name,
                    "display_name": user.display_name,
                    "title": user.title,
                    "email": None,
                    "is_bot": user.is_bot,
                    "is_deleted": user.is_deleted,
                    "is_restricted": user.is_restricted,
                    "is_ultra_restricted": user.is_ultra_restricted,
                    "is_external": user.is_external,
                    "tz": user.tz,
                    "raw": user.raw,
                    "workspace_key": user.workspace_key,
                    "first_seen": seen_at,
                    "last_seen": seen_at,
                },
            )
        if commit:
            self._conn.commit()

    def upsert_sighting_only(
        self,
        *,
        channel_id: str,
        ts: str,
        content_sha256: str,
        device_id: str,
        source: str,
        seen_at: str,
        snapshot_id: str,
        state_sha256: str,
        commit: bool = True,
        first_seen: str | None = None,
        bump_count: bool = True,
    ) -> None:
        first = first_seen or seen_at
        if bump_count:
            conflict_sql = """
              last_seen=MAX(message_sightings.last_seen, excluded.last_seen),
              first_seen=MIN(message_sightings.first_seen, excluded.first_seen),
              sightings=message_sightings.sightings + 1,
              last_snapshot_id=excluded.last_snapshot_id,
              last_state_sha256=excluded.last_state_sha256
            """
        else:
            conflict_sql = """
              last_seen=MAX(message_sightings.last_seen, excluded.last_seen),
              first_seen=MIN(message_sightings.first_seen, excluded.first_seen),
              last_snapshot_id=excluded.last_snapshot_id,
              last_state_sha256=excluded.last_state_sha256
            """
        self._conn.execute(
            f"""
            INSERT INTO message_sightings(
              channel_id, ts, content_sha256, device_id, source,
              first_seen, last_seen, sightings, first_snapshot_id, last_snapshot_id,
              last_state_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(channel_id, ts, content_sha256, device_id, source) DO UPDATE SET
              {conflict_sql}
            """,
            (
                channel_id,
                ts,
                content_sha256,
                device_id,
                source,
                first,
                seen_at,
                snapshot_id,
                snapshot_id,
                state_sha256,
            ),
        )
        if commit:
            self._conn.commit()

    def merge_version_sighting_times(
        self,
        *,
        channel_id: str,
        ts: str,
        content_sha256: str,
        first_seen: str,
        last_seen: str,
        commit: bool = True,
    ) -> None:
        unknown = {"", "1970-01-01T00:00:00Z"}
        # Ignore unknown incoming bounds; also treat stored epoch as unknown.
        use_first = first_seen not in unknown
        use_last = last_seen not in unknown
        if not use_first and not use_last:
            return
        if use_first:
            self._conn.execute(
                """
                UPDATE message_versions SET
                  first_seen=CASE
                    WHEN first_seen IS NULL OR first_seen='' OR first_seen='1970-01-01T00:00:00Z'
                      OR first_seen > ?
                    THEN ? ELSE first_seen END
                WHERE channel_id=? AND ts=? AND content_sha256=?
                """,
                (first_seen, first_seen, channel_id, ts, content_sha256),
            )
        if use_last:
            self._conn.execute(
                """
                UPDATE message_versions SET
                  last_seen=CASE
                    WHEN last_seen IS NULL OR last_seen='' OR last_seen='1970-01-01T00:00:00Z'
                      OR last_seen < ?
                    THEN ? ELSE last_seen END
                WHERE channel_id=? AND ts=? AND content_sha256=?
                """,
                (last_seen, last_seen, channel_id, ts, content_sha256),
            )
        if commit:
            self._conn.commit()

    def reconcile_version_times_from_sightings(
        self,
        *,
        channel_id: str,
        ts: str,
        content_sha256: str,
        commit: bool = True,
    ) -> None:
        """After a version arrives, pull bounds from any earlier sighting rows."""
        row = self._conn.execute(
            """
            SELECT MIN(first_seen) AS first_seen, MAX(last_seen) AS last_seen
            FROM message_sightings
            WHERE channel_id=? AND ts=? AND content_sha256=?
            """,
            (channel_id, ts, content_sha256),
        ).fetchone()
        if row is None or not row["last_seen"]:
            return
        self.merge_version_sighting_times(
            channel_id=channel_id,
            ts=ts,
            content_sha256=content_sha256,
            first_seen=str(row["first_seen"] or row["last_seen"]),
            last_seen=str(row["last_seen"]),
            commit=commit,
        )

    def ingest_observation(
        self,
        obs: MessageObservation,
        *,
        device_id: str,
        snapshot_id: str,
        state_sha256: str,
        seen_at: str,
        hlc: str,
        write_change_log: bool = True,
        write_sighting: bool = True,
        commit: bool = True,
        reproject: bool = True,
    ) -> bool:
        """Upsert version (+ optional sighting). Returns True if a new message_versions row was inserted."""
        existed = self._conn.execute(
            """
            SELECT 1 FROM message_versions
            WHERE channel_id=? AND ts=? AND content_sha256=?
            """,
            (obs.channel_id, obs.ts, obs.content_sha256),
        ).fetchone()
        raw = json.dumps(obs.raw, ensure_ascii=False)
        self._conn.execute(
            """
            INSERT INTO message_versions(
              channel_id, ts, content_sha256, edited_ts, is_deletion, is_partial,
              source_rank, user_id, thread_ts, subtype, text, search_text, raw_json,
              first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id, ts, content_sha256) DO UPDATE SET
              last_seen=MAX(message_versions.last_seen, excluded.last_seen),
              source_rank=MAX(message_versions.source_rank, excluded.source_rank),
              is_partial=MIN(message_versions.is_partial, excluded.is_partial),
              is_deletion=MAX(message_versions.is_deletion, excluded.is_deletion)
            """,
            (
                obs.channel_id,
                obs.ts,
                obs.content_sha256,
                obs.edited_ts,
                1 if obs.is_deletion else 0,
                1 if obs.is_partial else 0,
                obs.source_rank,
                obs.user_id,
                obs.thread_ts,
                obs.subtype,
                obs.text,
                obs.search_text,
                raw,
                seen_at,
                seen_at,
            ),
        )
        if write_sighting:
            self._conn.execute(
                """
                INSERT INTO message_sightings(
                  channel_id, ts, content_sha256, device_id, source,
                  first_seen, last_seen, sightings, first_snapshot_id, last_snapshot_id,
                  last_state_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(channel_id, ts, content_sha256, device_id, source) DO UPDATE SET
                  last_seen=excluded.last_seen,
                  sightings=message_sightings.sightings + 1,
                  last_snapshot_id=excluded.last_snapshot_id,
                  last_state_sha256=excluded.last_state_sha256
                """,
                (
                    obs.channel_id,
                    obs.ts,
                    obs.content_sha256,
                    device_id,
                    obs.source,
                    seen_at,
                    seen_at,
                    snapshot_id,
                    snapshot_id,
                    state_sha256,
                ),
            )
        if write_change_log:
            # Version content only (stable). Sightings are a separate outbox entity.
            version_payload = {
                "channel_id": obs.channel_id,
                "ts": obs.ts,
                "workspace_key": obs.workspace_key,
                "content_sha256": obs.content_sha256,
                "edited_ts": obs.edited_ts,
                "is_deletion": obs.is_deletion,
                "is_partial": obs.is_partial,
                "source_rank": obs.source_rank,
                "user_id": obs.user_id,
                "thread_ts": obs.thread_ts,
                "subtype": obs.subtype,
                "text": obs.text,
                "search_text": obs.search_text,
                "raw": obs.raw,
                "source": obs.source,
                "reply_count": obs.reply_count,
                "latest_reply": obs.latest_reply,
                "reply_users": obs.reply_users,
            }
            entity_key = f"{obs.channel_id}|{obs.ts}|{obs.content_sha256}"
            self.append_entity_change(
                hlc=hlc,
                device_id=device_id,
                entity="message_version",
                entity_key=entity_key,
                payload=version_payload,
            )
            if write_sighting:
                sighting_payload = {
                    "channel_id": obs.channel_id,
                    "ts": obs.ts,
                    "content_sha256": obs.content_sha256,
                    "device_id": device_id,
                    "source": obs.source,
                    "first_seen": seen_at,
                    "last_seen": seen_at,
                    "snapshot_id": snapshot_id,
                    "state_sha256": state_sha256,
                }
                sighting_key = (
                    f"{obs.channel_id}|{obs.ts}|{obs.content_sha256}|{device_id}|{obs.source}"
                )
                self.append_entity_change(
                    hlc=hlc,
                    device_id=device_id,
                    entity="sighting",
                    entity_key=sighting_key,
                    payload=sighting_payload,
                )
        # thread meta
        if obs.thread_ts or (obs.reply_count and obs.reply_count > 0):
            root = obs.thread_ts or obs.ts
            self._conn.execute(
                """
                INSERT INTO threads(
                  channel_id, thread_ts, reply_count_reported, replies_captured,
                  subscribed, last_reply_ts, updated_hlc
                ) VALUES (?, ?, ?, 0, ?, ?, ?)
                ON CONFLICT(channel_id, thread_ts) DO UPDATE SET
                  reply_count_reported=MAX(
                    COALESCE(threads.reply_count_reported, 0),
                    COALESCE(excluded.reply_count_reported, 0)
                  ),
                  subscribed=COALESCE(excluded.subscribed, threads.subscribed),
                  last_reply_ts=MAX(
                    COALESCE(threads.last_reply_ts, ''),
                    COALESCE(excluded.last_reply_ts, '')
                  ),
                  updated_hlc=excluded.updated_hlc
                """,
                (
                    obs.channel_id,
                    root,
                    obs.reply_count,
                    1 if bool(obs.raw.get("_subscribed") or obs.raw.get("subscribed")) else None,
                    obs.latest_reply or obs.ts,
                    hlc,
                ),
            )
        if commit:
            self._conn.commit()
        if reproject:
            self.reproject_message(obs.channel_id, obs.ts)
        return existed is None

    def reproject_message(
        self, channel_id: str, ts: str, *, commit: bool = True
    ) -> dict[str, Any] | None:
        rows = self._conn.execute(
            """
            SELECT * FROM message_versions WHERE channel_id=? AND ts=?
            """,
            (channel_id, ts),
        ).fetchall()
        if not rows:
            return None
        versions = [
            VersionCandidate(
                channel_id=r["channel_id"],
                ts=r["ts"],
                content_sha256=r["content_sha256"],
                edited_ts=r["edited_ts"],
                is_partial=bool(r["is_partial"]),
                is_deletion=bool(r["is_deletion"]),
                source_rank=r["source_rank"],
                last_seen=r["last_seen"],
                text=r["text"],
                search_text=r["search_text"],
                user_id=r["user_id"],
                thread_ts=r["thread_ts"],
                subtype=r["subtype"],
            )
            for r in rows
        ]
        chosen = choose_current(versions)
        if chosen is None:
            return None
        deleted = deleted_state_for(versions, chosen)
        reply_count, latest_reply, reply_users = max_merge_thread_meta(versions)
        # workspace from channel table
        ch = self._conn.execute(
            "SELECT workspace_key FROM channels WHERE channel_id=?",
            (channel_id,),
        ).fetchone()
        workspace_key = ch["workspace_key"] if ch else "unknown"
        first_seen = min(r["first_seen"] for r in rows)
        last_seen = max(r["last_seen"] for r in rows)
        is_reply = 1 if chosen.thread_ts and chosen.thread_ts != ts else 0
        has_links = 1 if "http://" in chosen.text or "https://" in chosen.text else 0
        has_files = 0
        try:
            raw_obj = json.loads(
                self._conn.execute(
                    "SELECT raw_json FROM message_versions WHERE channel_id=? AND ts=? AND content_sha256=?",
                    (channel_id, ts, chosen.content_sha256),
                ).fetchone()["raw_json"]
            )
            files = raw_obj.get("files") if isinstance(raw_obj, dict) else None
            if isinstance(files, list) and files:
                has_files = 1
        except (TypeError, KeyError, json.JSONDecodeError):
            has_files = 0
        # Upsert messages
        previous_thread_ts: str | None = None
        existing = self._conn.execute(
            "SELECT rowid, thread_ts FROM messages WHERE channel_id=? AND ts=?",
            (channel_id, ts),
        ).fetchone()
        if existing:
            previous_thread_ts = existing["thread_ts"]
            rowid = existing["rowid"]
            self._conn.execute("DELETE FROM messages_fts WHERE rowid=?", (rowid,))
            self._conn.execute("DELETE FROM messages_trigram WHERE rowid=?", (rowid,))
            self._conn.execute(
                """
                UPDATE messages SET
                  ts_num=?, workspace_key=?, user_id=?, thread_ts=?, is_reply=?,
                  subtype=?, text=?, search_text=?, current_sha256=?, version_count=?,
                  edited_ts=?, deleted_state=?, reply_count=?, latest_reply=?,
                  reply_users_json=?, has_files=?, has_links=?, first_seen=?, last_seen=?
                WHERE rowid=?
                """,
                (
                    _ts_num(ts),
                    workspace_key,
                    chosen.user_id,
                    chosen.thread_ts,
                    is_reply,
                    chosen.subtype,
                    chosen.text,
                    chosen.search_text,
                    chosen.content_sha256,
                    len(versions),
                    chosen.edited_ts,
                    deleted,
                    reply_count,
                    latest_reply,
                    json.dumps(reply_users),
                    has_files,
                    has_links,
                    first_seen,
                    last_seen,
                    rowid,
                ),
            )
        else:
            cur = self._conn.execute(
                """
                INSERT INTO messages(
                  channel_id, ts, ts_num, workspace_key, user_id, thread_ts, is_reply,
                  subtype, text, search_text, current_sha256, version_count, edited_ts,
                  deleted_state, reply_count, latest_reply, reply_users_json,
                  has_files, has_links, first_seen, last_seen
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    channel_id,
                    ts,
                    _ts_num(ts),
                    workspace_key,
                    chosen.user_id,
                    chosen.thread_ts,
                    is_reply,
                    chosen.subtype,
                    chosen.text,
                    chosen.search_text,
                    chosen.content_sha256,
                    len(versions),
                    chosen.edited_ts,
                    deleted,
                    reply_count,
                    latest_reply,
                    json.dumps(reply_users),
                    has_files,
                    has_links,
                    first_seen,
                    last_seen,
                ),
            )
            rowid = cur.lastrowid
        author = chosen.user_id or ""
        chan_name = channel_id
        crow = self._conn.execute(
            "SELECT name FROM channels WHERE channel_id=?", (channel_id,)
        ).fetchone()
        if crow and crow["name"]:
            chan_name = crow["name"]
        self._conn.execute(
            "INSERT INTO messages_fts(rowid, search_text, author, channel) VALUES (?, ?, ?, ?)",
            (rowid, chosen.search_text, author, chan_name),
        )
        self._conn.execute(
            "INSERT INTO messages_trigram(rowid, search_text) VALUES (?, ?)",
            (rowid, chosen.search_text),
        )
        meta = {
            "previous_thread_ts": previous_thread_ts,
            "is_new_msg": existing is None,
            "thread_ts": chosen.thread_ts,
            "deleted_state": deleted,
        }
        if commit:
            self._conn.commit()
            self.refresh_message_search_docs(
                channel_id,
                ts,
                previous_thread_ts=previous_thread_ts,
                is_new_msg=existing is None,
            )
        return meta

    def refresh_message_search_docs(
        self,
        channel_id: str,
        ts: str,
        *,
        previous_thread_ts: str | None = None,
        is_new_msg: bool | None = None,
        commit: bool = True,
    ) -> None:
        """Best-effort search_docs refresh after a durable message projection."""
        try:
            from slack_cache_mcp.search.docs import SearchDocsBuilder

            row = self._conn.execute(
                """
                SELECT thread_ts, deleted_state, text FROM messages
                WHERE channel_id=? AND ts=?
                """,
                (channel_id, ts),
            ).fetchone()
            if row is None:
                return
            builder = SearchDocsBuilder(self)
            thread_root = row["thread_ts"] or ts
            is_root = thread_root == ts
            deleted = row["deleted_state"] or "none"
            text_changed = True
            if deleted != "none":
                builder.drop_message_doc(channel_id, ts)
            else:
                _doc_id, text_changed = builder.upsert_message_doc(channel_id, ts)
            builder.refresh_aggregates(
                channel_id,
                thread_root,
                message_ts=ts,
                text_changed=text_changed or deleted != "none",
                previous_thread_ts=previous_thread_ts,
                is_new=bool(is_new_msg) if is_new_msg is not None else False,
            )
            if is_root:
                builder.refresh_reply_docs_after_parent_change(channel_id, ts)
            if commit:
                self._conn.commit()
        except Exception:
            # In transactional apply (commit=False), propagate so the outbox marker
            # rolls back and retry can rebuild search docs.
            if not commit:
                raise
            pass

    def list_workspaces(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM workspaces ORDER BY name").fetchall()
        out = []
        for r in rows:
            msg_count = self._conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE workspace_key=?",
                (r["workspace_key"],),
            ).fetchone()["c"]
            ch_count = self._conn.execute(
                "SELECT COUNT(*) AS c FROM channels WHERE workspace_key=?",
                (r["workspace_key"],),
            ).fetchone()["c"]
            out.append(
                {
                    "key": r["workspace_key"],
                    "name": r["name"],
                    "domain": r["domain"],
                    "self_is_guest": bool(r["self_is_guest"]),
                    "channels_known": ch_count,
                    "messages": msg_count,
                }
            )
        return out

    def list_channels(
        self,
        *,
        workspace: str | None = None,
        query: str | None = None,
        kinds: list[str] | None = None,
        only_with_messages: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = "SELECT c.* FROM channels c WHERE 1=1"
        params: list[Any] = []
        if workspace:
            sql += " AND c.workspace_key=?"
            params.append(workspace)
        if kinds:
            sql += f" AND c.kind IN ({','.join('?' for _ in kinds)})"
            params.extend(kinds)
        if query:
            sql += " AND (c.name LIKE ? OR c.channel_id=?)"
            params.extend([f"%{query}%", query])
        if only_with_messages:
            sql += " AND EXISTS (SELECT 1 FROM messages m WHERE m.channel_id=c.channel_id)"
        sql += " ORDER BY c.name LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "id": r["channel_id"],
                "name": r["name"],
                "kind": r["kind"],
                "workspace_key": r["workspace_key"],
                "is_ext_shared": bool(r["is_ext_shared"]),
            }
            for r in rows
        ]

    def _msg_dict(self, r: sqlite3.Row) -> dict[str, Any]:
        return {
            "channel_id": r["channel_id"],
            "ts": r["ts"],
            "workspace_key": r["workspace_key"],
            "user_id": r["user_id"],
            "thread_ts": r["thread_ts"],
            "text": r["text"],
            "subtype": r["subtype"],
            "edited_ts": r["edited_ts"],
            "deleted_state": r["deleted_state"],
            "version_count": r["version_count"],
            "ref": f"slk://{r['workspace_key']}/{r['channel_id']}/{r['ts']}",
        }

    def get_message(
        self,
        *,
        channel: str,
        ts: str,
        versions: bool = False,
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM messages WHERE channel_id=? AND ts=?",
            (channel, ts),
        ).fetchone()
        if not row:
            return None
        out = self._msg_dict(row)
        if versions:
            vers = self._conn.execute(
                """
                SELECT content_sha256, edited_ts, is_deletion, source_rank, first_seen, last_seen, text
                FROM message_versions WHERE channel_id=? AND ts=?
                ORDER BY last_seen
                """,
                (channel, ts),
            ).fetchall()
            sights = self._conn.execute(
                """
                SELECT content_sha256, device_id, source, first_seen, last_seen, sightings
                FROM message_sightings WHERE channel_id=? AND ts=?
                """,
                (channel, ts),
            ).fetchall()
            out["versions"] = [dict(v) for v in vers]
            out["sightings"] = [dict(s) for s in sights]
        return out

    def read_channel(
        self,
        channel: str,
        *,
        after: str | None = None,
        before: str | None = None,
        limit: int = 50,
        order: str = "newest",
        include_replies: bool = False,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM messages WHERE channel_id=?"
        params: list[Any] = [channel]
        if not include_replies:
            sql += " AND (thread_ts IS NULL OR thread_ts=ts)"
        if after:
            sql += " AND ts_num >= ?"
            params.append(_ts_num(after))
        if before:
            sql += " AND ts_num < ?"
            params.append(_ts_num(before))
        sql += " ORDER BY ts_num " + ("DESC" if order == "newest" else "ASC")
        sql += " LIMIT ?"
        params.append(limit)
        return [self._msg_dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def get_thread(
        self,
        channel: str,
        thread_ts: str,
        *,
        limit: int = 200,
    ) -> dict[str, Any]:
        root = self.get_message(channel=channel, ts=thread_ts)
        replies = self._conn.execute(
            """
            SELECT * FROM messages
            WHERE channel_id=? AND thread_ts=? AND ts!=?
            ORDER BY ts_num ASC LIMIT ?
            """,
            (channel, thread_ts, thread_ts, limit),
        ).fetchall()
        meta = self._conn.execute(
            "SELECT * FROM threads WHERE channel_id=? AND thread_ts=?",
            (channel, thread_ts),
        ).fetchone()
        return {
            "root": root,
            "replies": [self._msg_dict(r) for r in replies],
            "missing_parent": root is None,
            "replies_captured": len(replies),
            "reply_count_reported": meta["reply_count_reported"] if meta else None,
        }

    def get_context(
        self,
        channel: str,
        ts: str,
        *,
        before: int = 5,
        after: int = 5,
    ) -> dict[str, Any]:
        anchor = self.get_message(channel=channel, ts=ts)
        earlier = self._conn.execute(
            """
            SELECT * FROM messages WHERE channel_id=? AND ts_num < ?
            ORDER BY ts_num DESC LIMIT ?
            """,
            (channel, _ts_num(ts), before),
        ).fetchall()
        later = self._conn.execute(
            """
            SELECT * FROM messages WHERE channel_id=? AND ts_num > ?
            ORDER BY ts_num ASC LIMIT ?
            """,
            (channel, _ts_num(ts), after),
        ).fetchall()
        msgs = list(reversed([self._msg_dict(r) for r in earlier]))
        if anchor:
            msgs.append(anchor)
        msgs.extend(self._msg_dict(r) for r in later)
        return {
            "messages": msgs,
            "has_more_before": len(earlier) == before,
            "has_more_after": len(later) == after,
        }

    def resolve(
        self,
        name_or_id: str,
        *,
        kind: str = "any",
        workspace: str | None = None,
    ) -> list[dict[str, Any]]:
        raw = name_or_id.strip()
        q = raw.lstrip("#@")
        wants_dm = raw.startswith("@") or kind == "im"
        out: list[dict[str, Any]] = []
        if kind in ("any", "channel", "im"):
            sql = "SELECT * FROM channels WHERE channel_id=? OR name=? OR name LIKE ?"
            params: list[Any] = [q, q, f"%{q}%"]
            if workspace:
                sql += " AND workspace_key=?"
                params.append(workspace)
            for r in self._conn.execute(sql + " LIMIT 20", params).fetchall():
                conf = 1.0 if r["channel_id"] == q or r["name"] == q else 0.7
                out.append(
                    {
                        "id": r["channel_id"],
                        "kind": "channel",
                        "name": r["name"],
                        "workspace": r["workspace_key"],
                        "confidence": conf,
                    }
                )
            # PLAN §10.5: in:@user resolves to IM channel via im_user_id
            if wants_dm or kind in ("any", "im"):
                user_hits = self._conn.execute(
                    "SELECT user_id FROM users WHERE user_id=? OR name=? OR display_name=?",
                    (q, q, q),
                ).fetchall()
                user_ids = [r["user_id"] for r in user_hits] or ([q] if q.startswith("U") else [])
                for uid in user_ids:
                    im_sql = "SELECT * FROM channels WHERE kind='im' AND im_user_id=?"
                    im_params: list[Any] = [uid]
                    if workspace:
                        im_sql += " AND workspace_key=?"
                        im_params.append(workspace)
                    for r in self._conn.execute(im_sql + " LIMIT 5", im_params).fetchall():
                        out.append(
                            {
                                "id": r["channel_id"],
                                "kind": "channel",
                                "name": r["name"] or f"@{uid}",
                                "workspace": r["workspace_key"],
                                "confidence": 0.95,
                            }
                        )
        if kind in ("any", "user"):
            sql = "SELECT * FROM users WHERE user_id=? OR name=? OR display_name=? OR real_name LIKE ?"
            params = [q, q, q, f"%{q}%"]
            for r in self._conn.execute(sql + " LIMIT 20", params).fetchall():
                conf = 1.0 if r["user_id"] == q or r["name"] == q else 0.6
                out.append(
                    {
                        "id": r["user_id"],
                        "kind": "user",
                        "name": r["display_name"] or r["name"],
                        "workspace": workspace,
                        "confidence": conf,
                    }
                )
        if kind in ("any", "workspace"):
            for r in self._conn.execute(
                "SELECT * FROM workspaces WHERE workspace_key=? OR name=? OR domain=?",
                (q, q, q),
            ).fetchall():
                out.append(
                    {
                        "id": r["workspace_key"],
                        "kind": "workspace",
                        "name": r["name"],
                        "workspace": r["workspace_key"],
                        "confidence": 1.0,
                    }
                )
        out.sort(key=lambda x: -x["confidence"])
        return out

    def get_user(self, user: str, workspace: str | None = None) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM users WHERE user_id=?", (user,)).fetchone()
        if not row:
            # try name
            row = self._conn.execute(
                "SELECT * FROM users WHERE name=? OR display_name=?",
                (user, user),
            ).fetchone()
        if not row:
            return None
        wss = [
            r["workspace_key"]
            for r in self._conn.execute(
                "SELECT workspace_key FROM user_workspaces WHERE user_id=?",
                (row["user_id"],),
            ).fetchall()
        ]
        return {
            "id": row["user_id"],
            "name": row["name"],
            "real_name": row["real_name"],
            "display_name": row["display_name"],
            "is_bot": bool(row["is_bot"]),
            "is_restricted": bool(row["is_restricted"]),
            "is_ultra_restricted": bool(row["is_ultra_restricted"]),
            "is_external": bool(row["is_external"]),
            "workspaces": wss,
        }

    def cache_status(
        self, workspace: str | None = None, channel: str | None = None
    ) -> dict[str, Any]:
        snap = self._conn.execute(
            "SELECT * FROM snapshots ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        msg_sql = "SELECT COUNT(*) AS c FROM messages"
        params: list[Any] = []
        if workspace:
            msg_sql += " WHERE workspace_key=?"
            params.append(workspace)
        count = self._conn.execute(msg_sql, params).fetchone()["c"]
        gens = self._conn.execute("SELECT COUNT(*) AS c FROM state_generations").fetchone()["c"]
        suspected = self._conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE deleted_state='suspected'"
        ).fetchone()["c"]
        deleted = self._conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE deleted_state='deleted'"
        ).fetchone()["c"]
        coverage_n = self._conn.execute("SELECT COUNT(*) AS c FROM coverage_ranges").fetchone()["c"]
        # Change metrics: recent snapshot stats
        recent = self._conn.execute(
            """
            SELECT trigger, COUNT(*) AS c,
                   SUM(states_decoded) AS states,
                   SUM(COALESCE(json_extract(stats_json, '$.generations_new'), 0)) AS gens_new
            FROM snapshots
            WHERE finished_at IS NOT NULL
            GROUP BY trigger
            """
        ).fetchall()
        by_trigger = {
            r["trigger"]: {
                "snapshots": r["c"],
                "states_decoded": r["states"],
            }
            for r in recent
        }
        channel_block: dict[str, Any] | None = None
        if channel:
            st = self._conn.execute(
                "SELECT * FROM channel_status WHERE channel_id=?", (channel,)
            ).fetchone()
            ranges = self._conn.execute(
                "SELECT start_ts, end_ts, contiguous_to_latest FROM coverage_ranges WHERE channel_id=?",
                (channel,),
            ).fetchall()
            channel_block = {
                "status": dict(st) if st else None,
                "coverage_ranges": [dict(r) for r in ranges],
            }
        devices = []
        for d in self.list_devices():
            sightings = self._conn.execute(
                "SELECT COUNT(*) AS c FROM message_sightings WHERE device_id=?",
                (d["device_id"],),
            ).fetchone()["c"]
            changes = self._conn.execute(
                "SELECT COUNT(*) AS c FROM change_log WHERE device_id=?",
                (d["device_id"],),
            ).fetchone()["c"]
            devices.append(
                {
                    "device_id": d["device_id"],
                    "label": d["label"],
                    "has_public_key": bool(d.get("public_key")),
                    "sightings": sightings,
                    "change_log_rows": changes,
                }
            )
        from slack_cache_mcp.sync.state_hash import compute_state_hash

        try:
            state_hash = compute_state_hash(self)
        except Exception:
            state_hash = None
        sources = [
            {
                "source_id": r["source_id"],
                "kind": r["kind"],
                "path_hint": r["path_hint"],
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"],
            }
            for r in self._conn.execute(
                """
                SELECT source_id, kind, path_hint, first_seen, last_seen
                FROM sources
                ORDER BY kind, path_hint
                """
            ).fetchall()
        ]
        return {
            "messages": count,
            "workspaces": len(self.list_workspaces()),
            "state_generations": gens,
            "deleted": deleted,
            "suspected": suspected,
            "coverage_ranges": coverage_n,
            "snapshots_by_trigger": by_trigger,
            "last_snapshot": dict(snap) if snap else None,
            # Keep channel as the caller-provided channel id (stable type: str|None)
            "channel": channel,
            # Provide structured per-channel info separately (stable type: dict|None)
            "channel_status": channel_block,
            "devices": devices,
            "sources": sources,
            "state_hash": state_hash,
            "schema_version": SCHEMA_VERSION,
        }

    def search_fts(
        self,
        match_sql: str,
        *,
        limit: int,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        filt = dict(filters or {})
        if "deleted_mode" not in filt:
            filt["deleted_mode"] = "include" if filt.get("include_deleted") else "exclude"
        clause, fparams = _filter_clause(filt)
        sql = f"""
        SELECT m.*, bm25(messages_fts) AS rank
        FROM messages_fts
        JOIN messages m ON m.rowid = messages_fts.rowid
        WHERE messages_fts MATCH ?
        {clause}
        ORDER BY rank LIMIT ?
        """
        params: list[Any] = [match_sql, *fparams, limit]
        try:
            rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []
        return [{**self._msg_dict(r), "score": -float(r["rank"])} for r in rows]

    def search_trigram(
        self,
        needle: str,
        *,
        limit: int,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        filt = dict(filters or {})
        if "deleted_mode" not in filt:
            filt["deleted_mode"] = "include" if filt.get("include_deleted") else "exclude"
        clause, fparams = _filter_clause(filt)
        q = '"' + needle.replace('"', "") + '"'
        sql = f"""
        SELECT m.*
        FROM messages_trigram
        JOIN messages m ON m.rowid = messages_trigram.rowid
        WHERE messages_trigram MATCH ?
        {clause}
        LIMIT ?
        """
        params: list[Any] = [q, *fparams, limit]
        try:
            rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []
        return [self._msg_dict(r) for r in rows]

    def search_filtered(
        self,
        *,
        limit: int,
        filters: dict[str, Any] | None = None,
        order: str = "DESC",
    ) -> list[dict[str, Any]]:
        """Filter-only search when the query has operators but no free text."""
        filt = dict(filters or {})
        if "deleted_mode" not in filt:
            filt["deleted_mode"] = "include" if filt.get("include_deleted") else "exclude"
        clause, fparams = _filter_clause(filt)
        direction = "ASC" if str(order).upper() == "ASC" else "DESC"
        sql = f"""
        SELECT m.*
        FROM messages m
        WHERE 1=1
        {clause}
        ORDER BY m.ts_num {direction}
        LIMIT ?
        """
        rows = self._conn.execute(sql, [*fparams, limit]).fetchall()
        return [self._msg_dict(r) for r in rows]

    def rebuild_fts(self) -> dict[str, int]:
        """Rebuild messages_fts and messages_trigram from the current messages view."""
        self._conn.execute("DELETE FROM messages_fts")
        self._conn.execute("DELETE FROM messages_trigram")
        rows = self._conn.execute(
            "SELECT rowid, search_text, user_id, channel_id FROM messages"
        ).fetchall()
        n = 0
        for r in rows:
            chan = r["channel_id"]
            crow = self._conn.execute(
                "SELECT name FROM channels WHERE channel_id=?", (chan,)
            ).fetchone()
            chan_name = crow["name"] if crow and crow["name"] else chan
            self._conn.execute(
                "INSERT INTO messages_fts(rowid, search_text, author, channel) VALUES (?, ?, ?, ?)",
                (r["rowid"], r["search_text"], r["user_id"] or "", chan_name),
            )
            self._conn.execute(
                "INSERT INTO messages_trigram(rowid, search_text) VALUES (?, ?)",
                (r["rowid"], r["search_text"]),
            )
            n += 1
        self._conn.commit()
        return {"rows": n}

    def embedding_coverage(self) -> dict[str, Any]:
        from slack_cache_mcp.store.sqlite.vec import try_load_sqlite_vec

        vec_ok = try_load_sqlite_vec(self._conn)
        has_docs = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='search_docs'"
        ).fetchone()
        has_vec = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='docs_vec'"
        ).fetchone()
        has_blob = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='doc_embeddings'"
        ).fetchone()
        pending = embedded = 0
        if has_docs:
            pending = int(
                self._conn.execute(
                    "SELECT COUNT(*) AS c FROM search_docs WHERE embedded_model IS NULL"
                ).fetchone()["c"]
            )
            embedded = int(
                self._conn.execute(
                    "SELECT COUNT(*) AS c FROM search_docs WHERE embedded_model IS NOT NULL"
                ).fetchone()["c"]
            )
        return {
            "vec_available": bool(has_docs and (has_blob or (vec_ok and has_vec))),
            "sqlite_vec": bool(vec_ok and has_vec),
            "pending": pending,
            "embedded": embedded,
        }

    def upsert_file(self, f: FileMeta, hlc: str, seen_at: str) -> None:
        raw = json.dumps(f.raw, ensure_ascii=False)
        self._conn.execute(
            """
            INSERT INTO files(
              file_id, workspace_key, user_id, name, title, mimetype, filetype, size,
              created, preview_text, local_path, local_sha256, ocr_text, raw_json,
              first_seen, last_seen, updated_hlc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(file_id) DO UPDATE SET
              name=COALESCE(excluded.name, files.name),
              title=COALESCE(excluded.title, files.title),
              preview_text=COALESCE(excluded.preview_text, files.preview_text),
              local_path=COALESCE(excluded.local_path, files.local_path),
              local_sha256=COALESCE(excluded.local_sha256, files.local_sha256),
              ocr_text=COALESCE(excluded.ocr_text, files.ocr_text),
              raw_json=excluded.raw_json,
              last_seen=excluded.last_seen,
              updated_hlc=excluded.updated_hlc
            """,
            (
                f.file_id,
                f.workspace_key,
                f.user_id,
                f.name,
                f.title,
                f.mimetype,
                f.filetype,
                f.size,
                f.created,
                f.preview_text,
                f.local_path,
                f.local_sha256,
                f.ocr_text,
                raw,
                seen_at,
                seen_at,
                hlc,
            ),
        )
        self._conn.commit()

    def upsert_reaction(self, rx: ReactionSet, hlc: str) -> None:
        self._conn.execute(
            """
            INSERT INTO reactions(channel_id, ts, emoji, count, users_json, observed_hlc)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id, ts, emoji) DO UPDATE SET
              count=excluded.count,
              users_json=excluded.users_json,
              observed_hlc=excluded.observed_hlc
            """,
            (rx.channel_id, rx.ts, rx.emoji, rx.count, json.dumps(rx.users), hlc),
        )
        self._conn.commit()

    def upsert_saved_item(self, item: SavedItem, seen_at: str) -> None:
        self._conn.execute(
            """
            INSERT INTO saved_items(
              workspace_key, item_type, item_id, channel_id, ts, state,
              date_created, date_due, raw_json, first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workspace_key, item_type, item_id) DO UPDATE SET
              channel_id=COALESCE(excluded.channel_id, saved_items.channel_id),
              ts=COALESCE(excluded.ts, saved_items.ts),
              state=COALESCE(excluded.state, saved_items.state),
              raw_json=excluded.raw_json,
              last_seen=excluded.last_seen
            """,
            (
                item.workspace_key,
                item.item_type,
                item.item_id,
                item.channel_id,
                item.ts,
                item.state,
                item.date_created,
                item.date_due,
                json.dumps(item.raw, ensure_ascii=False),
                seen_at,
                seen_at,
            ),
        )
        self._conn.commit()

    def upsert_canvas(self, doc: CanvasDoc, seen_at: str) -> None:
        self._conn.execute(
            """
            INSERT INTO canvases(
              canvas_id, workspace_key, channel_id, title, text, raw_json,
              content_sha256, first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(canvas_id) DO UPDATE SET
              title=COALESCE(excluded.title, canvases.title),
              text=excluded.text,
              raw_json=excluded.raw_json,
              content_sha256=excluded.content_sha256,
              last_seen=excluded.last_seen
            """,
            (
                doc.canvas_id,
                doc.workspace_key,
                doc.channel_id,
                doc.title,
                doc.text,
                json.dumps(doc.raw, ensure_ascii=False),
                doc.content_sha256,
                seen_at,
                seen_at,
            ),
        )
        self._conn.execute(
            """
            INSERT OR IGNORE INTO canvas_versions(canvas_id, content_sha256, text, first_seen)
            VALUES (?, ?, ?, ?)
            """,
            (doc.canvas_id, doc.content_sha256, doc.text, seen_at),
        )
        self._conn.commit()

    def upsert_draft(self, draft: Draft, seen_at: str) -> None:
        self._conn.execute(
            """
            INSERT INTO drafts(
              workspace_key, draft_id, channel_id, thread_ts, text, last_updated,
              raw_json, first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workspace_key, draft_id) DO UPDATE SET
              text=excluded.text,
              raw_json=excluded.raw_json,
              last_seen=excluded.last_seen
            """,
            (
                draft.workspace_key,
                draft.draft_id,
                draft.channel_id,
                draft.thread_ts,
                draft.text,
                draft.last_updated,
                json.dumps(draft.raw, ensure_ascii=False),
                seen_at,
                seen_at,
            ),
        )
        self._conn.commit()

    def record_source_yield(
        self,
        snapshot_id: str,
        source_name: str,
        *,
        messages: int = 0,
        files: int = 0,
        canvases: int = 0,
        reactions: int = 0,
        saved_items: int = 0,
        drafts: int = 0,
        cache_entries: int = 0,
        errors: int = 0,
        extras: dict[str, Any] | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO source_yield(
              snapshot_id, source_name, messages, files, canvases, reactions,
              saved_items, drafts, cache_entries, errors, extras_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_id, source_name) DO UPDATE SET
              messages=excluded.messages,
              files=excluded.files,
              canvases=excluded.canvases,
              reactions=excluded.reactions,
              saved_items=excluded.saved_items,
              drafts=excluded.drafts,
              cache_entries=excluded.cache_entries,
              errors=excluded.errors,
              extras_json=excluded.extras_json
            """,
            (
                snapshot_id,
                source_name,
                messages,
                files,
                canvases,
                reactions,
                saved_items,
                drafts,
                cache_entries,
                errors,
                json.dumps(extras or {}),
            ),
        )
        self._conn.commit()

    def source_yield_report(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT source_name,
                   SUM(messages) AS messages,
                   SUM(files) AS files,
                   SUM(canvases) AS canvases,
                   SUM(reactions) AS reactions,
                   SUM(saved_items) AS saved_items,
                   SUM(drafts) AS drafts,
                   SUM(cache_entries) AS cache_entries,
                   SUM(errors) AS errors
            FROM source_yield
            GROUP BY source_name
            ORDER BY messages DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_files(
        self,
        *,
        workspace: str | None = None,
        query: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM files WHERE 1=1"
        params: list[Any] = []
        if workspace:
            sql += " AND workspace_key=?"
            params.append(workspace)
        if query:
            sql += " AND (name LIKE ? OR title LIKE ? OR preview_text LIKE ?)"
            q = f"%{query}%"
            params.extend([q, q, q])
        sql += " ORDER BY last_seen DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def list_saved(
        self,
        *,
        workspace: str | None = None,
        state: str = "any",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM saved_items WHERE 1=1"
        params: list[Any] = []
        if workspace:
            sql += " AND workspace_key=?"
            params.append(workspace)
        if state != "any":
            sql += " AND state=?"
            params.append(state)
        sql += " ORDER BY last_seen DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def list_canvases(
        self,
        *,
        workspace: str | None = None,
        query: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        sql = "SELECT canvas_id, workspace_key, channel_id, title, content_sha256, first_seen, last_seen, length(text) AS text_len FROM canvases WHERE 1=1"
        params: list[Any] = []
        if workspace:
            sql += " AND workspace_key=?"
            params.append(workspace)
        if query:
            sql += " AND (title LIKE ? OR text LIKE ?)"
            q = f"%{query}%"
            params.extend([q, q])
        sql += " ORDER BY last_seen DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def get_canvas(self, canvas_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM canvases WHERE canvas_id=?", (canvas_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_messages_for_export(
        self,
        *,
        workspace: str | None = None,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM messages WHERE 1=1"
        params: list[Any] = []
        if workspace:
            sql += " AND workspace_key=?"
            params.append(workspace)
        if not include_deleted:
            sql += " AND deleted_state='none'"
        sql += " ORDER BY channel_id, ts_num"
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def upsert_search_doc(
        self,
        *,
        kind: str,
        workspace_key: str,
        channel_id: str | None,
        anchor_ts: str | None,
        start_ts: str | None,
        end_ts: str | None,
        text: str,
        message_rowid: int | None = None,
    ) -> int:
        text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        existing = self._conn.execute(
            """
            SELECT doc_id, text_sha256 FROM search_docs
            WHERE kind=? AND workspace_key=?
              AND channel_id IS ?
              AND anchor_ts IS ?
              AND start_ts IS ?
              AND end_ts IS ?
            """,
            (kind, workspace_key, channel_id, anchor_ts, start_ts, end_ts),
        ).fetchone()
        if existing:
            if existing["text_sha256"] == text_sha:
                return int(existing["doc_id"])
            self._conn.execute(
                """
                UPDATE search_docs SET
                  text=?, text_sha256=?, embedded_model=NULL, embedded_at=NULL,
                  message_rowid=COALESCE(?, message_rowid)
                WHERE doc_id=?
                """,
                (text, text_sha, message_rowid, int(existing["doc_id"])),
            )
            with contextlib.suppress(sqlite3.OperationalError):
                self._conn.execute(
                    "DELETE FROM docs_vec WHERE doc_id=?", (int(existing["doc_id"]),)
                )
            return int(existing["doc_id"])
        cur = self._conn.execute(
            """
            INSERT INTO search_docs(
              kind, workspace_key, channel_id, anchor_ts, start_ts, end_ts,
              text, text_sha256, message_rowid
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                kind,
                workspace_key,
                channel_id,
                anchor_ts,
                start_ts,
                end_ts,
                text,
                text_sha,
                message_rowid,
            ),
        )
        return int(cur.lastrowid or 0)

    def _hit_passes_filters(
        self,
        hit: dict[str, Any],
        filt: dict[str, Any],
        *,
        kinds: list[str] | None,
        doc_kind: str | None,
    ) -> bool:
        if kinds and doc_kind and doc_kind not in kinds:
            return False
        if filt.get("channel_ids") and hit.get("channel_id") not in filt["channel_ids"]:
            return False
        if filt.get("workspace_key") and hit.get("workspace_key") != filt["workspace_key"]:
            return False
        deleted_mode = filt.get("deleted_mode", "exclude")
        ds = hit.get("deleted_state") or "none"
        if deleted_mode == "exclude" and ds != "none":
            return False
        if deleted_mode == "only" and ds == "none":
            return False
        if filt.get("user_ids") and hit.get("user_id") not in filt["user_ids"]:
            return False
        if filt.get("after") is not None:
            try:
                if float(hit.get("ts") or 0) < float(filt["after"]):
                    return False
            except (TypeError, ValueError):
                return False
        if filt.get("before") is not None:
            try:
                if float(hit.get("ts") or 0) >= float(filt["before"]):
                    return False
            except (TypeError, ValueError):
                return False
        # Re-check message-level predicates via a lightweight SQL probe when possible.
        if hit.get("channel_id") and hit.get("ts"):
            clause, params = _filter_clause(filt)
            row = self._conn.execute(
                f"SELECT 1 FROM messages m WHERE m.channel_id=? AND m.ts=? {clause}",
                [hit["channel_id"], hit["ts"], *params],
            ).fetchone()
            if not row:
                return False
        return True

    def search_vector(
        self,
        query_vec: Any,
        *,
        limit: int,
        filters: dict[str, Any] | None = None,
        kinds: list[str] | None = None,
        model_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """KNN over docs_vec or numpy ``doc_embeddings`` fallback."""
        from slack_cache_mcp.store.sqlite.vec import knn_docs, knn_docs_numpy, try_load_sqlite_vec

        has_docs = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='search_docs'"
        ).fetchone()
        if not has_docs:
            return []

        filt = dict(filters or {})
        if kinds and len(kinds) == 1:
            filt["kind"] = kinds[0]
        if model_id:
            filt["model_id"] = model_id

        has_vec = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='docs_vec'"
        ).fetchone()
        use_vec = bool(has_vec and try_load_sqlite_vec(self._conn))
        blob_n = 0
        with contextlib.suppress(sqlite3.OperationalError):
            blob_n = int(
                self._conn.execute(
                    "SELECT COUNT(*) AS c FROM doc_embeddings"
                ).fetchone()["c"]
            )

        def _consume(
            pairs: list[tuple[int, float]], *, out: list[dict[str, Any]], seen: set[int]
        ) -> None:
            for doc_id, distance in pairs:
                if doc_id in seen:
                    continue
                seen.add(doc_id)
                # Prefer authoritative blob distance when present.
                if blob_n > 0:
                    brow = self._conn.execute(
                        "SELECT embedding FROM doc_embeddings WHERE doc_id=?",
                        (doc_id,),
                    ).fetchone()
                    if brow is not None:
                        import numpy as np

                        from slack_cache_mcp.search.embedder import cosine_similarity

                        emb = np.frombuffer(brow["embedding"], dtype=np.float32)
                        distance = 1.0 - float(
                            cosine_similarity(
                                np.asarray(query_vec, dtype=np.float32), emb
                            )
                        )
                doc = self._conn.execute(
                    "SELECT * FROM search_docs WHERE doc_id=?", (doc_id,)
                ).fetchone()
                if not doc:
                    continue
                if model_id and doc["embedded_model"] != model_id:
                    continue
                hit = self._doc_to_search_hit(dict(doc), distance=distance)
                if hit is None:
                    continue
                if not self._hit_passes_filters(
                    hit, filt, kinds=kinds, doc_kind=doc["kind"]
                ):
                    continue
                out.append(hit)
                if len(out) >= limit:
                    return

        def _eligible_count(candidates: dict[int, float], *, need: int) -> int:
            n = 0
            for doc_id, distance in sorted(candidates.items(), key=lambda x: x[1]):
                doc = self._conn.execute(
                    "SELECT * FROM search_docs WHERE doc_id=?", (doc_id,)
                ).fetchone()
                if not doc:
                    continue
                if model_id and doc["embedded_model"] != model_id:
                    continue
                hit = self._doc_to_search_hit(dict(doc), distance=distance)
                if hit is None:
                    continue
                if not self._hit_passes_filters(
                    hit, filt, kinds=kinds, doc_kind=doc["kind"]
                ):
                    continue
                n += 1
                if n >= need:
                    return n
            return n

        best: dict[int, float] = {}
        vec_k_max = 4096
        if use_vec:
            k_pool = min(max(limit * 20, 200), vec_k_max)
            while True:
                pairs = knn_docs(self._conn, query_vec, k=k_pool, filters=filt)
                for doc_id, distance in pairs:
                    prev = best.get(doc_id)
                    if prev is None or distance < prev:
                        best[doc_id] = distance
                # Expand until enough *eligible* hits (post author/date filters).
                if (
                    _eligible_count(best, need=limit) >= limit
                    or k_pool >= vec_k_max
                    or len(pairs) < k_pool
                ):
                    break
                k_pool = min(k_pool * 2, vec_k_max)
            # Rescore vec candidates with authoritative blob distance (O(k)).
            if blob_n > 0 and best:
                import numpy as np

                from slack_cache_mcp.search.embedder import cosine_similarity

                q = np.asarray(query_vec, dtype=np.float32)
                for doc_id in list(best):
                    brow = self._conn.execute(
                        "SELECT embedding FROM doc_embeddings WHERE doc_id=?",
                        (doc_id,),
                    ).fetchone()
                    if brow is not None:
                        emb = np.frombuffer(brow["embedding"], dtype=np.float32)
                        best[doc_id] = 1.0 - float(cosine_similarity(q, emb))

        # Blob scan when vec is unavailable, blob-only/divergent docs exist, or
        # vec+filters still cannot fill the limit. Expand until eligible hits
        # satisfy ``limit`` (author/date/has:* filters apply after distance rank).
        from slack_cache_mcp.store.sqlite.vec import docs_vec_needs_reconcile

        need_blob_scan = False
        divergent = False
        if blob_n > 0:
            if not use_vec:
                need_blob_scan = True
            else:
                blob_only = 0
                with contextlib.suppress(sqlite3.OperationalError):
                    blob_only = int(
                        self._conn.execute(
                            """
                            SELECT COUNT(*) AS c FROM doc_embeddings e
                            WHERE NOT EXISTS (
                              SELECT 1 FROM docs_vec v WHERE v.doc_id = e.doc_id
                            )
                            """
                        ).fetchone()["c"]
                    )
                divergent = docs_vec_needs_reconcile(self._conn)
                if (
                    blob_only > 0
                    or divergent
                    or _eligible_count(best, need=limit) < limit
                ):
                    need_blob_scan = True
        if need_blob_scan:
            # Any predicate not pushed into knn SQL can hide nearer blobs.
            sql_keys = {
                "workspace_key",
                "channel_ids",
                "kind",
                "model_id",
                "deleted_mode",
                "include_deleted",
            }
            post_filter = any(
                k not in sql_keys and v not in (None, False, [], "")
                for k, v in filt.items()
            )
            # Unrestricted when no vec, post-filters, or known divergence;
            # otherwise expand a bounded pool until eligible hits fill.
            if not use_vec or post_filter or divergent:
                pairs = knn_docs_numpy(
                    self._conn,
                    query_vec,
                    k=None,
                    filters=filt,
                    kinds=kinds,
                    model_id=model_id,
                )
                for doc_id, distance in pairs:
                    prev = best.get(doc_id)
                    if prev is None or distance < prev:
                        best[doc_id] = distance
            else:
                blob_k = max(limit * 20, 200)
                while True:
                    pairs = knn_docs_numpy(
                        self._conn,
                        query_vec,
                        k=blob_k,
                        filters=filt,
                        kinds=kinds,
                        model_id=model_id,
                    )
                    for doc_id, distance in pairs:
                        prev = best.get(doc_id)
                        if prev is None or distance < prev:
                            best[doc_id] = distance
                    if (
                        _eligible_count(best, need=limit) >= limit
                        or blob_k >= blob_n
                        or len(pairs) < blob_k
                    ):
                        break
                    blob_k = min(blob_k * 2, blob_n)
        out: list[dict[str, Any]] = []
        seen: set[int] = set()
        ranked = sorted(best.items(), key=lambda x: x[1])
        _consume(ranked, out=out, seen=seen)
        return out

    def _doc_to_search_hit(
        self, doc: dict[str, Any], *, distance: float
    ) -> dict[str, Any] | None:
        score = 1.0 / (1.0 + max(0.0, float(distance)))
        if doc["kind"] == "message" and doc.get("channel_id") and doc.get("anchor_ts"):
            msg = self.get_message(channel=doc["channel_id"], ts=doc["anchor_ts"])
            if not msg:
                return None
            return {**msg, "score": score, "doc_kind": "message", "doc_id": doc["doc_id"]}
        # Non-message docs: surface as synthetic hits anchored when possible.
        if doc.get("channel_id") and doc.get("anchor_ts") and doc["kind"] in {
            "thread",
            "window",
        }:
            msg = self.get_message(channel=doc["channel_id"], ts=doc["anchor_ts"])
            if msg:
                return {
                    **msg,
                    "score": score,
                    "doc_kind": doc["kind"],
                    "doc_id": doc["doc_id"],
                    "doc_text": doc["text"][:500],
                }
        return {
            "channel_id": doc.get("channel_id") or "",
            "ts": doc.get("anchor_ts") or "",
            "workspace_key": doc["workspace_key"],
            "user_id": None,
            "thread_ts": None,
            "text": doc["text"][:500],
            "subtype": None,
            "edited_ts": None,
            "deleted_state": "none",
            "version_count": 1,
            "ref": f"slk://{doc['workspace_key']}/{doc.get('channel_id') or '-'}/{doc.get('anchor_ts') or '-'}",
            "score": score,
            "doc_kind": doc["kind"],
            "doc_id": doc["doc_id"],
        }

    def list_threads(
        self,
        *,
        workspace: str | None = None,
        channel: str | None = None,
        subscribed_only: bool = False,
        active_after: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        from slack_cache_mcp.search.query import parse_time_bound

        sql = """
            SELECT t.*,
                   COALESCE(m.workspace_key, c.workspace_key) AS workspace_key,
                   m.text AS root_text,
                   m.user_id,
                   (
                     SELECT COUNT(*) FROM messages r
                     WHERE r.channel_id=t.channel_id
                       AND r.thread_ts=t.thread_ts
                       AND r.ts!=t.thread_ts
                   ) AS replies_captured_live
            FROM threads t
            LEFT JOIN messages m ON m.channel_id=t.channel_id AND m.ts=t.thread_ts
            LEFT JOIN channels c ON c.channel_id=t.channel_id
            WHERE 1=1
        """
        params: list[Any] = []
        if workspace:
            sql += " AND COALESCE(m.workspace_key, c.workspace_key)=?"
            params.append(workspace)
        if channel:
            sql += " AND t.channel_id=?"
            params.append(channel)
        if subscribed_only:
            sql += " AND t.subscribed=1"
        if active_after:
            sql += " AND CAST(COALESCE(t.last_reply_ts, t.thread_ts) AS REAL) >= ?"
            try:
                params.append(float(active_after))
            except ValueError:
                params.append(parse_time_bound(active_after, end=False))
        sql += " ORDER BY CAST(COALESCE(t.last_reply_ts, t.thread_ts) AS REAL) DESC LIMIT ?"
        params.append(limit)
        out: list[dict[str, Any]] = []
        for r in self._conn.execute(sql, params).fetchall():
            d = dict(r)
            d["replies_captured"] = int(d.pop("replies_captured_live") or 0)
            out.append(d)
        return out

    def whats_new(
        self,
        *,
        since: str | None = None,
        workspace: str | None = None,
        limit: int = 50,
        watermark_store: Path | None = None,
    ) -> dict[str, Any]:
        """Messages since watermark; advances a scoped, insertion-ordered cursor."""
        meta_key = "whats_new_watermark" + (f":{workspace}" if workspace else "")
        if not since:
            since = self._read_whats_new_cursor(meta_key, watermark_store)
        lim = min(limit, 200)
        use_rowid = since.startswith("rowid:") or since.isdigit()
        insert_bound: int | None = None
        if use_rowid:
            after_rowid = 0
            if since.startswith("rowid:"):
                with contextlib.suppress(ValueError):
                    after_rowid = int(since.split(":", 1)[1])
            else:
                after_rowid = int(since)
            sql = """
                SELECT * FROM messages
                WHERE deleted_state='none' AND rowid > ?
            """
            params: list[Any] = [after_rowid]
            order = " ORDER BY rowid ASC LIMIT ?"
        else:
            # Finish legacy first_seen order (with ch/ts tie-break) before rowid.
            # Carry an insertion boundary so concurrent inserts behind the cursor
            # are drained via rowid after legacy completes (not skipped).
            body = since[7:] if since.startswith("legacy:") else since
            bound_parts = body.split("#rowbound:")
            body_main = bound_parts[0]
            if len(bound_parts) == 2:
                with contextlib.suppress(ValueError):
                    insert_bound = int(bound_parts[1])
            if insert_bound is None:
                insert_bound = self._max_message_rowid()
            chunks = body_main.split("|")
            cur_fs = chunks[0]
            # Restrict legacy pages to the insertion boundary so concurrent
            # inserts (rowid > bound) are not returned twice after the switch.
            if len(chunks) >= 3:
                sql = """
                    SELECT * FROM messages
                    WHERE deleted_state='none'
                      AND rowid <= ?
                      AND (
                        first_seen > ?
                        OR (first_seen = ? AND channel_id > ?)
                        OR (first_seen = ? AND channel_id = ? AND ts > ?)
                      )
                """
                params = [
                    insert_bound,
                    cur_fs,
                    cur_fs,
                    chunks[1],
                    cur_fs,
                    chunks[1],
                    chunks[2],
                ]
            else:
                sql = """
                    SELECT * FROM messages
                    WHERE first_seen >= ? AND deleted_state='none' AND rowid <= ?
                """
                params = [cur_fs, insert_bound]
            order = " ORDER BY first_seen ASC, channel_id ASC, ts ASC LIMIT ?"
        if workspace:
            sql += " AND workspace_key=?"
            params.append(workspace)
        sql += order
        params.append(lim + 1)
        raw = list(self._conn.execute(sql, params).fetchall())
        has_more = len(raw) > lim
        page = raw[:lim]
        rows = [self._msg_dict(r) | {"first_seen": r["first_seen"]} for r in page]
        grouped: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            grouped.setdefault(r["channel_id"], []).append(r)
        if page:
            last = page[-1]
            if use_rowid:
                new_mark = f"rowid:{int(last['rowid'])}"
            elif has_more:
                new_mark = (
                    f"legacy:{last['first_seen']}|{last['channel_id']}|{last['ts']}"
                    f"#rowbound:{insert_bound}"
                )
            else:
                # Legacy drained: resume from the insertion boundary captured when
                # legacy pagination began so later inserts are not skipped.
                new_mark = f"rowid:{int(insert_bound or 0)}"
                # Clients that stop on has_more=false must be told more rows wait.
                post_sql = (
                    "SELECT 1 FROM messages WHERE deleted_state='none' AND rowid > ?"
                )
                post_params: list[Any] = [insert_bound]
                if workspace:
                    post_sql += " AND workspace_key=?"
                    post_params.append(workspace)
                post_sql += " LIMIT 1"
                if self._conn.execute(post_sql, post_params).fetchone():
                    has_more = True
        else:
            new_mark = since if use_rowid else f"rowid:{int(insert_bound or 0)}"
            if not use_rowid and insert_bound is not None:
                post_sql = (
                    "SELECT 1 FROM messages WHERE deleted_state='none' AND rowid > ?"
                )
                post_params = [insert_bound]
                if workspace:
                    post_sql += " AND workspace_key=?"
                    post_params.append(workspace)
                post_sql += " LIMIT 1"
                if self._conn.execute(post_sql, post_params).fetchone():
                    has_more = True
        if rows or not use_rowid:
            self._write_whats_new_cursor(meta_key, new_mark, watermark_store)
        return {
            "since": since,
            "watermark": new_mark,
            "has_more": has_more,
            "messages": rows,
            "by_channel": grouped,
        }

    def _max_message_rowid(self) -> int:
        row = self._conn.execute("SELECT MAX(rowid) AS r FROM messages").fetchone()
        return int(row["r"] or 0)

    def _dirty_window_map(self) -> dict[str, int]:
        """channel_id -> dirty generation (list form treated as generation 1)."""
        key = "window_dirty_channels"
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        if not row:
            return {}
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            parsed = json.loads(row["value"])
            if isinstance(parsed, dict):
                out: dict[str, int] = {}
                for k, v in parsed.items():
                    with contextlib.suppress(TypeError, ValueError):
                        out[str(k)] = int(v)
                return out
            if isinstance(parsed, list):
                return {str(c): 1 for c in parsed}
        return {}

    def _write_dirty_window_map(self, data: dict[str, int]) -> None:
        key = "window_dirty_channels"
        self._conn.execute(
            """
            INSERT INTO meta(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, json.dumps(data)),
        )

    def _begin_immediate(self) -> bool:
        """Start BEGIN IMMEDIATE. True if we opened it (caller may commit).

        Only treats 'within a transaction' as already-locked; lock timeouts raise.
        """
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            return True
        except sqlite3.OperationalError as exc:
            if "within a transaction" in str(exc).lower():
                return False
            raise

    _EMBED_OP_LOCK_META_KEY = "embedding_op_lock"  # dual-protocol with older builds
    _LEGACY_EMBED_OP_OWNER_RE = re.compile(r"^(?:cli-rebuild|daemon)-(\d+)$")

    @staticmethod
    def _process_alive(pid: int) -> bool:
        """Best-effort liveness for legacy-only meta holders during upgrades."""
        if pid <= 0:
            return False
        if sys.platform == "win32":
            import ctypes

            process_query_limited_information = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                process_query_limited_information, False, int(pid)
            )
            if not handle:
                # Distinguish access-denied (live) from missing (dead).
                err = ctypes.GetLastError()
                return err == 5  # ERROR_ACCESS_DENIED
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def _embed_lock_paths(self) -> list[Path]:
        """Sidecar paths deduplicated by file identity (not string equality).

        Includes an inode-keyed lock, path-string sidecars, and any existing
        ``*embedding_op*.lock`` already in the DB directory (so an older build
        holding a symlink alias sidecar blocks us instead of overlapping).
        """
        resolved_db = self.path.resolve()
        parent = resolved_db.parent
        candidates: list[Path] = []
        with contextlib.suppress(OSError):
            st = resolved_db.stat()
            candidates.append(
                parent / f".embedding_op.{st.st_dev}-{st.st_ino}.lock"
            )
        candidates.append(Path(str(resolved_db) + ".embedding_op.lock"))
        candidates.append(Path(str(self.path) + ".embedding_op.lock"))
        # Only this archive's basename sidecars — never other DBs in the dir.
        stem = resolved_db.name
        with contextlib.suppress(OSError):
            direct = parent / f"{stem}.embedding_op.lock"
            candidates.append(direct)
        unique: list[Path] = []
        for p in candidates:
            p.parent.mkdir(parents=True, exist_ok=True)
            if not p.exists():
                with contextlib.suppress(OSError):
                    p.write_text("0", encoding="utf-8")
            dup = False
            for u in unique:
                try:
                    if os.path.samefile(p, u):
                        dup = True
                        break
                except OSError:
                    if p.resolve() == u.resolve():
                        dup = True
                        break
            if not dup:
                unique.append(p)
        return unique

    def _lock_embed_file(self, fh: TextIO) -> None:
        """Exclusive, non-blocking byte-range / flock; raises OSError if busy."""
        fh.seek(0)
        if fh.read(1) == "":
            fh.write("0")
            fh.flush()
        fh.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_embed_file(self, fh: TextIO) -> None:
        fh.seek(0)
        if sys.platform == "win32":
            import msvcrt

            with contextlib.suppress(OSError):
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            with contextlib.suppress(OSError):
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _probe_file_locks_busy(self) -> bool:
        """True if any dual-path sidecar is currently held by another process."""
        for path in self._embed_lock_paths():
            try:
                fh = path.open("a+")
            except OSError:
                continue
            try:
                self._lock_embed_file(fh)
            except OSError:
                fh.close()
                return True
            with contextlib.suppress(Exception):
                self._unlock_embed_file(fh)
            with contextlib.suppress(Exception):
                fh.close()
        return False

    def _parse_legacy_embed_meta(
        self, raw: str | None
    ) -> tuple[str, int | None, str | None] | None:
        """Return ``(owner, pid, protocol)`` for a dual-protocol meta lock row."""
        if not raw:
            return None
        with contextlib.suppress(json.JSONDecodeError, TypeError, ValueError):
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("owner"):
                pid_raw = data.get("pid")
                pid = int(pid_raw) if pid_raw is not None else None
                if pid is None:
                    m = self._LEGACY_EMBED_OP_OWNER_RE.match(str(data["owner"]))
                    if m:
                        pid = int(m.group(1))
                protocol = (
                    str(data["protocol"]) if data.get("protocol") else None
                )
                return str(data["owner"]), pid, protocol
        m = self._LEGACY_EMBED_OP_OWNER_RE.match(raw)
        if m:
            return raw, int(m.group(1)), None
        return raw, None, None

    _EMBED_OP_LOCK_LEASE_S = 120.0

    def _legacy_meta_blocks(self, owner: str, *, holding_files: bool) -> bool:
        """True if another live holder still owns the meta lock row.

        ``file+meta`` claims are authoritative only while the OS file lock is
        held. When we already hold the files, prior ``file+meta`` rows are
        superseded. Legacy-only rows still use PID liveness.
        """
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key=?", (self._EMBED_OP_LOCK_META_KEY,)
        ).fetchone()
        parsed = self._parse_legacy_embed_meta(
            str(row["value"]) if row else None
        )
        if not parsed:
            return False
        other, pid, protocol = parsed
        if other == owner:
            return False
        if protocol == "file+meta":
            if holding_files:
                return False
            return self._probe_file_locks_busy()
        if pid is not None:
            return self._process_alive(pid)
        # Opaque legacy row without PID: preserve exclusion.
        return True

    def _claim_legacy_meta(
        self, owner: str, *, holding_files: bool, lease_s: float | None = None
    ) -> bool:
        """Claim/refresh the meta lock under IMMEDIATE; False if blocked."""
        lease = self._EMBED_OP_LOCK_LEASE_S if lease_s is None else float(lease_s)
        started = self._begin_immediate()
        try:
            if self._legacy_meta_blocks(owner, holding_files=holding_files):
                if started:
                    self._conn.commit()
                return False
            # Short renewable expires_at so 918c1ec-era lease readers can
            # recover after a crash; renew_embedding_op_lock refreshes it.
            payload = json.dumps(
                {
                    "owner": owner,
                    "pid": os.getpid(),
                    "expires_at": time.time() + max(lease, 1.0),
                    "protocol": "file+meta",
                }
            )
            self._conn.execute(
                """
                INSERT INTO meta(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (self._EMBED_OP_LOCK_META_KEY, payload),
            )
            if started:
                self._conn.commit()
            return True
        except Exception:
            if started:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.rollback()
            raise

    def _clear_legacy_meta(self, owner: str) -> None:
        started = self._begin_immediate()
        try:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key=?",
                (self._EMBED_OP_LOCK_META_KEY,),
            ).fetchone()
            parsed = self._parse_legacy_embed_meta(
                str(row["value"]) if row else None
            )
            if parsed and parsed[0] == owner:
                self._conn.execute(
                    "DELETE FROM meta WHERE key=?",
                    (self._EMBED_OP_LOCK_META_KEY,),
                )
            if started:
                self._conn.commit()
        except Exception:
            if started:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.rollback()
            raise

    def _release_embed_file_lock(self) -> None:
        owner = self._embed_lock_owner
        fhs = list(self._embed_lock_fhs)
        self._embed_lock_fhs = []
        self._embed_lock_owner = None
        for fh in fhs:
            with contextlib.suppress(Exception):
                self._unlock_embed_file(fh)
            with contextlib.suppress(Exception):
                fh.close()
        if owner is not None:
            with contextlib.suppress(Exception):
                self._clear_legacy_meta(owner)

    def embedding_op_lock_held(self) -> bool:
        """True when the OS file lock or a live legacy meta holder is present."""
        if self._embed_lock_fhs:
            return True
        with contextlib.suppress(Exception):
            if self._legacy_meta_blocks(owner="", holding_files=False):
                return True
        return self._probe_file_locks_busy()

    def embedding_op_lock_owned(self, owner: str) -> bool:
        """True if this repository instance holds the lock for ``owner``."""
        return bool(self._embed_lock_fhs) and self._embed_lock_owner == owner

    def acquire_embedding_op_lock(
        self, owner: str, *, lease_s: float | None = None
    ) -> bool:
        """Acquire crash-released OS file locks, then dual-protocol meta claim.

        File locks are taken first so claim/steal races cannot leave an active
        file holder without metadata. ``lease_s`` is ignored.
        """
        del lease_s
        if self._embed_lock_fhs:
            return self._embed_lock_owner == owner
        fhs: list[TextIO] = []
        claimed_meta = False
        try:
            for path in self._embed_lock_paths():
                path.parent.mkdir(parents=True, exist_ok=True)
                fh: TextIO = path.open("a+")
                fhs.append(fh)
                self._lock_embed_file(fh)
            if not self._claim_legacy_meta(owner, holding_files=True):
                raise OSError("embedding_op_meta_blocked")
            claimed_meta = True
            self._embed_lock_fhs = fhs
            self._embed_lock_owner = owner
            return True
        except Exception:
            for fh in fhs:
                with contextlib.suppress(Exception):
                    self._unlock_embed_file(fh)
                with contextlib.suppress(Exception):
                    fh.close()
            if claimed_meta:
                with contextlib.suppress(Exception):
                    self._clear_legacy_meta(owner)
            return False

    def renew_embedding_op_lock(
        self, owner: str, *, lease_s: float | None = None
    ) -> bool:
        """Fence + refresh short meta lease while the OS lock is held.

        Ownership check and lease write happen under one IMMEDIATE transaction
        so a legacy worker cannot slip in between.
        """
        if not self.embedding_op_lock_owned(owner):
            return False
        lease = self._EMBED_OP_LOCK_LEASE_S if lease_s is None else float(lease_s)
        started = self._begin_immediate()
        try:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key=?",
                (self._EMBED_OP_LOCK_META_KEY,),
            ).fetchone()
            parsed = self._parse_legacy_embed_meta(
                str(row["value"]) if row else None
            )
            # Missing or foreign row = lost ownership (do not recreate).
            if not parsed or parsed[0] != owner:
                if started:
                    self._conn.commit()
                return False
            payload = json.dumps(
                {
                    "owner": owner,
                    "pid": os.getpid(),
                    "expires_at": time.time() + max(lease, 1.0),
                    "protocol": "file+meta",
                }
            )
            self._conn.execute(
                """
                INSERT INTO meta(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (self._EMBED_OP_LOCK_META_KEY, payload),
            )
            if started:
                self._conn.commit()
            return True
        except Exception:
            if started:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.rollback()
            raise

    def heartbeat_embedding_op_lock(self) -> bool:
        """Renew the short meta lease if this instance holds the op lock.

        Called from long rebuild/embed loops so 918c1ec-era lease readers
        cannot steal mid-operation.
        """
        owner = self._embed_lock_owner
        if not owner or not self._embed_lock_fhs:
            return True
        return self.renew_embedding_op_lock(owner)

    def release_embedding_op_lock(self, owner: str) -> None:
        """Release the OS file locks and dual-protocol meta row if owned."""
        if self._embed_lock_owner != owner:
            return
        self._release_embed_file_lock()

    def _ensure_dirty_seq_initialized(self) -> None:
        """Seed ``window_dirty_seq`` above legacy map gens; never overwrite."""
        key = "window_dirty_seq"
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        if row:
            return
        existing_max = max(self._dirty_window_map().values(), default=0)
        # INSERT OR IGNORE — a concurrent mark must not be clobbered by a
        # stale legacy seed computed before that mark committed.
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
            (key, str(existing_max)),
        )

    def _next_dirty_generation(self) -> int:
        """Monotonic generation that never restarts after acknowledgements."""
        self._ensure_dirty_seq_initialized()
        key = "window_dirty_seq"
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        current = int(row["value"]) if row else 0
        existing_max = max(self._dirty_window_map().values(), default=0)
        n = max(current, existing_max) + 1
        self._conn.execute(
            """
            INSERT INTO meta(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, str(n)),
        )
        return n

    def mark_windows_dirty(self, channel_id: str) -> None:
        """Queue a channel for deferred window-doc rebuild (bump generation)."""
        started = self._begin_immediate()
        try:
            data = self._dirty_window_map()
            data[channel_id] = self._next_dirty_generation()
            self._write_dirty_window_map(data)
            if started:
                self._conn.commit()
        except Exception:
            if started:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.rollback()
            raise

    def peek_dirty_window_channels(self) -> list[tuple[str, int]]:
        """Return (channel_id, generation) without clearing."""
        started = self._begin_immediate()
        try:
            self._ensure_dirty_seq_initialized()
            out = list(self._dirty_window_map().items())
            if started:
                self._conn.commit()
            return out
        except Exception:
            if started:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.rollback()
            raise

    def ack_dirty_window_channel(self, channel_id: str, generation: int) -> None:
        """Ack only if ``generation`` still matches (newer marks stay dirty)."""
        started = self._begin_immediate()
        try:
            self._ensure_dirty_seq_initialized()
            data = self._dirty_window_map()
            cur = data.get(channel_id)
            if cur is not None and int(cur) == int(generation):
                del data[channel_id]
                self._write_dirty_window_map(data)
            if started:
                self._conn.commit()
        except Exception:
            if started:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.rollback()
            raise

    def _read_whats_new_cursor(self, meta_key: str, store: Path | None) -> str:
        default = "1970-01-01T00:00:00Z"
        if store is not None:
            try:
                data = json.loads(store.read_text(encoding="utf-8"))
                val = data.get(meta_key)
                if isinstance(val, str) and val:
                    return val
            except (OSError, json.JSONDecodeError):
                pass
        if not self.read_only:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key=?", (meta_key,)
            ).fetchone()
            if row:
                return str(row["value"])
        return default

    def _write_whats_new_cursor(
        self, meta_key: str, value: str, store: Path | None
    ) -> None:
        if store is not None:
            data: dict[str, Any] = {}
            with contextlib.suppress(OSError, json.JSONDecodeError):
                data = json.loads(store.read_text(encoding="utf-8"))
            data[meta_key] = value
            store.parent.mkdir(parents=True, exist_ok=True)
            store.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return
        if self.read_only:
            return
        self._conn.execute(
            """
            INSERT INTO meta(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (meta_key, value),
        )
        self._conn.commit()

    def scan_for_token_leak(self) -> list[str]:
        """Return table names where xox* bytes appear (should be empty)."""
        offenders: list[str] = []
        for table in (
            "message_versions",
            "messages",
            "workspaces",
            "channels",
            "users",
            "change_log",
            "files",
            "saved_items",
            "canvases",
            "drafts",
            "search_docs",
        ):
            try:
                rows = self._conn.execute(f"SELECT * FROM {table}").fetchall()
            except sqlite3.Error:
                continue
            for r in rows:
                blob = " ".join("" if v is None else str(v) for v in r).encode("utf-8", "replace")
                if _XOX_RE.search(blob):
                    offenders.append(table)
                    break
        return offenders
