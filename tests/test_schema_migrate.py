"""Schema migration for existing archive DBs (vault_path / suspected_streaks)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from slack_cache_mcp.store.sqlite.repo import SCHEMA_VERSION, SqliteRepository


def test_migrate_adds_vault_path_and_bumps_version(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
        INSERT INTO meta(key, value) VALUES ('schema_version', '1');
        CREATE TABLE devices (
          device_id TEXT PRIMARY KEY, label TEXT NOT NULL, public_key TEXT, created_at TEXT NOT NULL
        ) STRICT;
        CREATE TABLE sources (
          source_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, kind TEXT NOT NULL,
          path_hint TEXT NOT NULL, slack_version TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
        ) STRICT;
        CREATE TABLE snapshots (
          snapshot_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, started_at TEXT NOT NULL,
          finished_at TEXT, trigger TEXT NOT NULL, files_copied INTEGER, bytes_copied INTEGER,
          states_decoded INTEGER, decode_errors INTEGER, stats_json TEXT NOT NULL
        ) STRICT;
        CREATE TABLE state_generations (
          state_sha256 TEXT PRIMARY KEY, workspace_key TEXT NOT NULL, self_user_id TEXT,
          blob_ref TEXT, idb_seq INTEGER, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
          first_snapshot_id TEXT NOT NULL
        ) STRICT;
        """
    )
    conn.commit()
    conn.close()

    repo = SqliteRepository(db)
    cols = {
        r[1]
        for r in repo._conn.execute("PRAGMA table_info(state_generations)").fetchall()  # noqa: SLF001
    }
    assert "vault_path" in cols
    ver = repo._conn.execute(  # noqa: SLF001
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()["value"]
    assert ver == SCHEMA_VERSION
    # suspected_streaks present
    tables = {
        r[0]
        for r in repo._conn.execute(  # noqa: SLF001
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "suspected_streaks" in tables
    assert "sync_cursors" in tables
    repo.close()
