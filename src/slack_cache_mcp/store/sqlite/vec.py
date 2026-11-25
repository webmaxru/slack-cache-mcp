"""sqlite-vec loading and docs_vec helpers (Windows-first)."""

from __future__ import annotations

import contextlib
import sqlite3
from typing import Any

import numpy as np

from slack_cache_mcp.search.embedder import DEFAULT_DIM, embedding_to_bytes


def try_load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Load sqlite-vec into ``conn``. Returns False if unavailable."""
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
    # Leave load_extension enabled: some Windows builds unregister modules
    # when the flag is cleared.
    return True


def ensure_docs_vec(conn: sqlite3.Connection, *, dim: int = DEFAULT_DIM) -> bool:
    """Create or rebuild ``docs_vec`` for ``dim``. Returns True when usable."""
    if not try_load_sqlite_vec(conn):
        return False
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='docs_vec'"
    ).fetchone()
    meta_dim = conn.execute(
        "SELECT value FROM meta WHERE key='embedding_dim'"
    ).fetchone()
    if row and meta_dim and int(meta_dim["value"]) == int(dim):
        return True
    if row and (not meta_dim or int(meta_dim["value"]) != int(dim)):
        conn.execute("DROP TABLE IF EXISTS docs_vec")
        conn.execute(
            "UPDATE search_docs SET embedded_model=NULL, embedded_at=NULL"
        )
    try:
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS docs_vec USING vec0(
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
    conn.execute(
        """
        INSERT INTO meta(key, value) VALUES ('embedding_dim', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (str(int(dim)),),
    )
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
    model_id: str = "",
) -> None:
    blob = embedding_to_bytes(embedding)
    dim = int(np.asarray(embedding, dtype=np.float32).reshape(-1).shape[0])
    # Always persist in doc_embeddings (portable fallback / primary on macOS).
    conn.execute(
        """
        INSERT INTO doc_embeddings(doc_id, dim, embedding, model_id)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(doc_id) DO UPDATE SET
          dim=excluded.dim,
          embedding=excluded.embedding,
          model_id=excluded.model_id
        """,
        (doc_id, dim, blob, model_id or ""),
    )
    if not try_load_sqlite_vec(conn):
        return
    if not ensure_docs_vec(conn, dim=dim):
        return
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute("DELETE FROM docs_vec WHERE doc_id = ?", (doc_id,))
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute(
            """
            INSERT INTO docs_vec(doc_id, embedding, workspace_key, channel_id, ts_num, kind)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                doc_id,
                blob,
                workspace_key,
                channel_id or "",
                float(ts_num) if ts_num is not None else 0.0,
                kind,
            ),
        )


def knn_docs_numpy(
    conn: sqlite3.Connection,
    query_vec: np.ndarray,
    *,
    k: int = 100,
    filters: dict[str, Any] | None = None,
    kinds: list[str] | None = None,
    model_id: str | None = None,
) -> list[tuple[int, float]]:
    """Cosine KNN over ``doc_embeddings`` (no extension required)."""
    from slack_cache_mcp.search.embedder import cosine_similarity

    filt = filters or {}
    mid = model_id or filt.get("model_id")
    sql = """
        SELECT e.doc_id, e.embedding, d.kind, d.workspace_key, d.channel_id
        FROM doc_embeddings e
        JOIN search_docs d ON d.doc_id = e.doc_id
        WHERE 1=1
    """
    params: list[Any] = []
    if mid:
        sql += " AND e.model_id=? AND d.embedded_model=?"
        params.extend([mid, mid])
    if filt.get("workspace_key"):
        sql += " AND d.workspace_key=?"
        params.append(filt["workspace_key"])
    if filt.get("channel_ids"):
        ids = list(filt["channel_ids"])
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        sql += f" AND d.channel_id IN ({placeholders})"
        params.extend(ids)
    if kinds and len(kinds) == 1:
        sql += " AND d.kind=?"
        params.append(kinds[0])
    elif filt.get("kind"):
        sql += " AND d.kind=?"
        params.append(filt["kind"])
    rows = conn.execute(sql, params).fetchall()
    q = np.asarray(query_vec, dtype=np.float32)
    scored: list[tuple[int, float]] = []
    for r in rows:
        if kinds and r["kind"] not in kinds:
            continue
        emb = np.frombuffer(r["embedding"], dtype=np.float32)
        sim = cosine_similarity(q, emb)
        # distance = 1 - cosine (matches docs_vec cosine distance convention)
        scored.append((int(r["doc_id"]), 1.0 - sim))
    scored.sort(key=lambda x: x[1])
    return scored[:k]


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
