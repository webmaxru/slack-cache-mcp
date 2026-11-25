"""sqlite-vec loading and docs_vec helpers (Windows-first)."""

from __future__ import annotations

import contextlib
import sqlite3
from typing import Any

import numpy as np

from slack_cache_mcp.search.embedder import DEFAULT_DIM, embedding_to_bytes


def try_load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Load sqlite-vec into ``conn``. Returns False if unavailable."""
    # Already usable?
    try:
        conn.execute("SELECT vec_version()").fetchone()
        return True
    except sqlite3.OperationalError:
        pass
    try:
        conn.enable_load_extension(True)
    except AttributeError:
        return False
    try:
        import sqlite_vec

        sqlite_vec.load(conn)
        conn.execute("SELECT vec_version()").fetchone()
    except Exception:
        with contextlib.suppress(Exception):
            conn.enable_load_extension(False)
        return False
    # Leave load_extension enabled: some Windows SQLite builds unregister
    # dynamically loaded modules when the flag is cleared.
    return True


def ensure_docs_vec(conn: sqlite3.Connection, *, dim: int = DEFAULT_DIM) -> bool:
    """Create ``docs_vec`` if missing. Returns True when vec search is usable."""
    if not try_load_sqlite_vec(conn):
        return False
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='docs_vec'"
    ).fetchone()
    if row:
        return True
    try:
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE docs_vec USING vec0(
              doc_id INTEGER PRIMARY KEY,
              embedding float[{int(dim)}] distance_metric=cosine,
              workspace_key TEXT partition key,
              channel_id TEXT,
              ts_num FLOAT,
              kind TEXT
            )
            """
        )
    except sqlite3.OperationalError:
        return False
    return True


def upsert_doc_embedding(
    conn: sqlite3.Connection,
    *,
    doc_id: int,
    embedding: np.ndarray,
    workspace_key: str,
    channel_id: str | None,
    ts_num: float | None,
    kind: str,
) -> None:
    if not try_load_sqlite_vec(conn):
        return
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute("DELETE FROM docs_vec WHERE doc_id = ?", (doc_id,))
    conn.execute(
        """
        INSERT INTO docs_vec(doc_id, embedding, workspace_key, channel_id, ts_num, kind)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            doc_id,
            embedding_to_bytes(embedding),
            workspace_key,
            channel_id or "",
            float(ts_num) if ts_num is not None else 0.0,
            kind,
        ),
    )


def knn_docs(
    conn: sqlite3.Connection,
    query_vec: np.ndarray,
    *,
    k: int = 100,
    filters: dict[str, Any] | None = None,
) -> list[tuple[int, float]]:
    """Return (doc_id, distance) pairs. Lower distance is better (cosine)."""
    if not try_load_sqlite_vec(conn):
        return []
    filt = filters or {}
    sql = """
        SELECT doc_id, distance
        FROM docs_vec
        WHERE embedding MATCH ?
          AND k = ?
    """
    params: list[Any] = [embedding_to_bytes(query_vec), int(k)]
    if filt.get("workspace_key"):
        sql += " AND workspace_key = ?"
        params.append(filt["workspace_key"])
    if filt.get("channel_ids"):
        ids = list(filt["channel_ids"])
        if not ids:
            return []
        placeholders = " OR ".join("channel_id = ?" for _ in ids)
        sql += f" AND ({placeholders})"
        params.extend(ids)
    if filt.get("kind"):
        sql += " AND kind = ?"
        params.append(filt["kind"])
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(int(r[0]), float(r[1])) for r in rows]
