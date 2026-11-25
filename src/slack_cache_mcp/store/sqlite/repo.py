"""SQLite ArchiveRepository implementation."""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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

SCHEMA_VERSION = "4"
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_XOX_RE = re.compile(rb"xox[a-z]-", re.I)


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ts_num(ts: str) -> float:
    try:
        return float(ts)
    except ValueError:
        return 0.0


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
    return sql, params


class SqliteRepository:
    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = Path(path)
        self.read_only = read_only
        if read_only:
            if not self.path.exists():
                raise FileNotFoundError(self.path)
            uri = f"file:{self.path.as_posix()}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        # Best-effort sqlite-vec; lexical search still works if unavailable.
        from slack_cache_mcp.store.sqlite.vec import ensure_docs_vec, try_load_sqlite_vec

        self._vec_ready = try_load_sqlite_vec(self._conn)
        if not read_only:
            self.ensure_schema()
            if self._vec_ready:
                ensure_docs_vec(self._conn)
        elif self._vec_ready:
            ensure_docs_vec(self._conn)

    def close(self) -> None:
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

    def upsert_device(self, device_id: str, label: str) -> None:
        now = _utc_now()
        self._conn.execute(
            """
            INSERT INTO devices(device_id, label, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET label=excluded.label
            """,
            (device_id, label, now),
        )
        self._conn.commit()

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
                    "SELECT deleted_state FROM messages WHERE channel_id=? AND ts=?",
                    (channel_id, ts),
                ).fetchone()
                if row and row["deleted_state"] == "none":
                    self._conn.execute(
                        "UPDATE messages SET deleted_state='suspected' WHERE channel_id=? AND ts=?",
                        (channel_id, ts),
                    )
                    marked.append(ts)
        self._conn.commit()
        return marked

    def upsert_workspace(self, ws: Workspace, hlc: str, seen_at: str) -> None:
        raw = json.dumps(ws.raw, ensure_ascii=False)
        self._conn.execute(
            """
            INSERT INTO workspaces(
              workspace_key, team_id, enterprise_id, name, domain, self_user_id,
              self_is_guest, raw_json, first_seen, last_seen, updated_hlc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workspace_key) DO UPDATE SET
              team_id=COALESCE(excluded.team_id, workspaces.team_id),
              enterprise_id=COALESCE(excluded.enterprise_id, workspaces.enterprise_id),
              name=COALESCE(excluded.name, workspaces.name),
              domain=COALESCE(excluded.domain, workspaces.domain),
              self_user_id=COALESCE(excluded.self_user_id, workspaces.self_user_id),
              self_is_guest=excluded.self_is_guest,
              raw_json=excluded.raw_json,
              last_seen=excluded.last_seen,
              updated_hlc=excluded.updated_hlc
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
            ),
        )
        self._conn.commit()

    def upsert_channel(self, ch: Channel, hlc: str, seen_at: str) -> None:
        raw = json.dumps(ch.raw, ensure_ascii=False)
        self._conn.execute(
            """
            INSERT INTO channels(
              channel_id, workspace_key, context_team_id, name, kind,
              is_ext_shared, is_archived, is_member, topic, purpose, im_user_id,
              raw_json, first_seen, last_seen, updated_hlc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
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
        self._conn.commit()

    def upsert_user(self, user: User, hlc: str, seen_at: str) -> None:
        raw = json.dumps(user.raw, ensure_ascii=False)
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
        self._conn.commit()

    def ingest_observation(
        self,
        obs: MessageObservation,
        *,
        device_id: str,
        snapshot_id: str,
        state_sha256: str,
        seen_at: str,
        hlc: str,
    ) -> bool:
        """Upsert version+sighting. Returns True if a new message_versions row was inserted."""
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
              source_rank=MAX(message_versions.source_rank, excluded.source_rank)
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
        payload = {
            "channel_id": obs.channel_id,
            "ts": obs.ts,
            "content_sha256": obs.content_sha256,
            "source": obs.source,
        }
        payload_json = json.dumps(payload, sort_keys=True)
        import hashlib

        payload_sha = hashlib.sha256(payload_json.encode()).hexdigest()
        entity_key = f"{obs.channel_id}|{obs.ts}|{obs.content_sha256}"
        self._conn.execute(
            """
            INSERT OR IGNORE INTO change_log(
              hlc, device_id, entity, entity_key, op, payload_json, payload_sha256
            ) VALUES (?, ?, 'message_version', ?, 'upsert', ?, ?)
            """,
            (hlc, device_id, entity_key, payload_json, payload_sha),
        )
        # thread meta
        if obs.thread_ts or (obs.reply_count and obs.reply_count > 0):
            root = obs.thread_ts or obs.ts
            self._conn.execute(
                """
                INSERT INTO threads(
                  channel_id, thread_ts, reply_count_reported, replies_captured,
                  last_reply_ts, updated_hlc
                ) VALUES (?, ?, ?, 0, ?, ?)
                ON CONFLICT(channel_id, thread_ts) DO UPDATE SET
                  reply_count_reported=MAX(
                    COALESCE(threads.reply_count_reported, 0),
                    COALESCE(excluded.reply_count_reported, 0)
                  ),
                  last_reply_ts=MAX(
                    COALESCE(threads.last_reply_ts, ''),
                    COALESCE(excluded.last_reply_ts, '')
                  ),
                  updated_hlc=excluded.updated_hlc
                """,
                (obs.channel_id, root, obs.reply_count, obs.latest_reply or obs.ts, hlc),
            )
        self._conn.commit()
        self.reproject_message(obs.channel_id, obs.ts)
        return existed is None

    def reproject_message(self, channel_id: str, ts: str) -> None:
        rows = self._conn.execute(
            """
            SELECT * FROM message_versions WHERE channel_id=? AND ts=?
            """,
            (channel_id, ts),
        ).fetchall()
        if not rows:
            return
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
            return
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
        existing = self._conn.execute(
            "SELECT rowid FROM messages WHERE channel_id=? AND ts=?",
            (channel_id, ts),
        ).fetchone()
        if existing:
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
        self._conn.commit()
        # Keep message search_docs in sync (embedding deferred).
        try:
            from slack_cache_mcp.search.docs import SearchDocsBuilder

            SearchDocsBuilder(self).upsert_message_doc(channel_id, ts)
            self._conn.commit()
        except Exception:
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
    ) -> list[dict[str, Any]]:
        """Filter-only search when the query has operators but no free text."""
        filt = dict(filters or {})
        if "deleted_mode" not in filt:
            filt["deleted_mode"] = "include" if filt.get("include_deleted") else "exclude"
        clause, fparams = _filter_clause(filt)
        sql = f"""
        SELECT m.*
        FROM messages m
        WHERE 1=1
        {clause}
        ORDER BY m.ts_num DESC
        LIMIT ?
        """
        rows = self._conn.execute(sql, [*fparams, limit]).fetchall()
        return [self._msg_dict(r) for r in rows]

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
              AND IFNULL(channel_id,'')=IFNULL(?, '')
              AND IFNULL(anchor_ts,'')=IFNULL(?, '')
              AND IFNULL(start_ts,'')=IFNULL(?, '')
              AND IFNULL(end_ts,'')=IFNULL(?, '')
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

    def search_vector(
        self,
        query_vec: Any,
        *,
        limit: int,
        filters: dict[str, Any] | None = None,
        kinds: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """KNN over docs_vec; map hits back to messages when possible."""
        from slack_cache_mcp.store.sqlite.vec import knn_docs

        filt = dict(filters or {})
        if kinds and len(kinds) == 1:
            filt["kind"] = kinds[0]
        pairs = knn_docs(self._conn, query_vec, k=max(limit * 3, limit), filters=filt)
        if not pairs and getattr(self, "_vec_ready", False):
            # Brute-force fallback for tiny corpora / missing vec rows.
            return self._search_vector_bruteforce(query_vec, limit=limit, filters=filt)
        out: list[dict[str, Any]] = []
        for doc_id, distance in pairs:
            doc = self._conn.execute(
                "SELECT * FROM search_docs WHERE doc_id=?", (doc_id,)
            ).fetchone()
            if not doc:
                continue
            if kinds and doc["kind"] not in kinds:
                continue
            if filt.get("channel_ids") and doc["channel_id"] not in filt["channel_ids"]:
                continue
            hit = self._doc_to_search_hit(dict(doc), distance=distance)
            if hit is None:
                continue
            # Apply message-level filters when we resolved a message.
            if filt and hit.get("channel_id"):
                if filt.get("deleted_mode") == "exclude" and hit.get("deleted_state") not in (
                    None,
                    "none",
                ):
                    continue
                if filt.get("user_ids") and hit.get("user_id") not in filt["user_ids"]:
                    continue
                if filt.get("after") is not None:
                    try:
                        if float(hit["ts"]) < float(filt["after"]):
                            continue
                    except (TypeError, ValueError):
                        pass
                if filt.get("before") is not None:
                    try:
                        if float(hit["ts"]) >= float(filt["before"]):
                            continue
                    except (TypeError, ValueError):
                        pass
            out.append(hit)
            if len(out) >= limit:
                break
        return out

    def _search_vector_bruteforce(
        self,
        query_vec: Any,
        *,
        limit: int,
        filters: dict[str, Any],
    ) -> list[dict[str, Any]]:
        import numpy as np

        from slack_cache_mcp.search.embedder import cosine_similarity

        rows = self._conn.execute(
            """
            SELECT d.*, v.embedding
            FROM search_docs d
            JOIN docs_vec v ON v.doc_id = d.doc_id
            WHERE d.embedded_model IS NOT NULL
            """
        ).fetchall()
        scored: list[tuple[float, dict[str, Any]]] = []
        q = np.asarray(query_vec, dtype=np.float32)
        for r in rows:
            if filters.get("workspace_key") and r["workspace_key"] != filters["workspace_key"]:
                continue
            if filters.get("channel_ids") and r["channel_id"] not in filters["channel_ids"]:
                continue
            emb = np.frombuffer(r["embedding"], dtype=np.float32)
            sim = cosine_similarity(q, emb)
            hit = self._doc_to_search_hit(dict(r), distance=1.0 - sim)
            if hit:
                scored.append((sim, hit))
        scored.sort(key=lambda x: -x[0])
        return [h for _, h in scored[:limit]]

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
        sql = """
            SELECT t.*, m.workspace_key, m.text AS root_text, m.user_id
            FROM threads t
            LEFT JOIN messages m ON m.channel_id=t.channel_id AND m.ts=t.thread_ts
            WHERE 1=1
        """
        params: list[Any] = []
        if workspace:
            sql += " AND m.workspace_key=?"
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
                params.append(_ts_num(active_after))
        sql += " ORDER BY CAST(COALESCE(t.last_reply_ts, t.thread_ts) AS REAL) DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def whats_new(
        self,
        *,
        since: str | None = None,
        workspace: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Messages whose first_seen is at/after ``since``, grouped by channel."""
        if not since:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='whats_new_watermark'"
            ).fetchone()
            since = row["value"] if row else "1970-01-01T00:00:00Z"
        sql = """
            SELECT * FROM messages
            WHERE first_seen >= ? AND deleted_state='none'
        """
        params: list[Any] = [since]
        if workspace:
            sql += " AND workspace_key=?"
            params.append(workspace)
        sql += " ORDER BY first_seen DESC LIMIT ?"
        params.append(min(limit, 200))
        rows = [self._msg_dict(r) | {"first_seen": r["first_seen"]} for r in self._conn.execute(sql, params).fetchall()]
        grouped: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            grouped.setdefault(r["channel_id"], []).append(r)
        watermark = _utc_now()
        if not self.read_only:
            self._conn.execute(
                """
                INSERT INTO meta(key, value) VALUES ('whats_new_watermark', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (watermark,),
            )
            self._conn.commit()
        return {
            "since": since,
            "watermark": watermark,
            "messages": rows,
            "by_channel": grouped,
        }

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
