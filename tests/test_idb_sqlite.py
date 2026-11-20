"""SQLite IndexedDB backend: identifier quoting + bounded scan."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from slack_cache_mcp.desktop.chromium.idb_sqlite import SqliteIdbBackend, _quote_ident


def test_quote_ident_escapes_quotes() -> None:
    assert _quote_ident("plain") == '"plain"'
    assert _quote_ident('a"b') == '"a""b"'


def test_scan_quoted_table_and_limit(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    idb = mirror / "IndexedDB"
    idb.mkdir(parents=True)
    db_path = idb / "synth.sqlite"
    conn = sqlite3.connect(str(db_path))
    # Weird table name that must be quoted
    conn.execute('CREATE TABLE "odd name"(key TEXT, value BLOB)')
    conn.execute(
        'INSERT INTO "odd name"(key, value) VALUES (?, ?)',
        ("k1", b"not-a-v8-payload"),
    )
    conn.commit()
    conn.close()

    backend = SqliteIdbBackend(mirror)
    recs = list(backend.iter_records())
    assert any(r.store == "odd name" for r in recs)
    # unwrap will fail on garbage bytes — error recorded, no crash
    odd = [r for r in recs if r.store == "odd name"][0]
    assert odd.error is not None or odd.value is None
