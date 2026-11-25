"""Background / on-demand embedding of pending search_docs."""

from __future__ import annotations

import contextlib
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from slack_cache_mcp.store.sqlite.vec import ensure_docs_vec, upsert_doc_embedding

if TYPE_CHECKING:
    from slack_cache_mcp.search.embedder import Embedder
    from slack_cache_mcp.store.sqlite.repo import SqliteRepository


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def embed_pending(
    repo: SqliteRepository,
    embedder: Embedder,
    *,
    batch_size: int = 64,
    limit: int | None = None,
) -> dict[str, Any]:
    """Embed search_docs rows pending for the active model."""
    # Prefer sqlite-vec when available; always have doc_embeddings fallback.
    ensure_docs_vec(repo._conn, dim=embedder.dim)  # noqa: SLF001

    prev = repo._conn.execute(  # noqa: SLF001
        "SELECT value FROM meta WHERE key='embedding_model'"
    ).fetchone()
    prev_dim = repo._conn.execute(  # noqa: SLF001
        "SELECT value FROM meta WHERE key='embedding_dim'"
    ).fetchone()
    if (prev and prev["value"] != embedder.model_id) or (
        prev_dim and int(prev_dim["value"]) != int(embedder.dim)
    ):
        repo._conn.execute(  # noqa: SLF001
            "UPDATE search_docs SET embedded_model=NULL, embedded_at=NULL"
        )
        with contextlib.suppress(sqlite3.OperationalError):
            repo._conn.execute("DELETE FROM docs_vec")  # noqa: SLF001
        with contextlib.suppress(sqlite3.OperationalError):
            repo._conn.execute("DELETE FROM doc_embeddings")  # noqa: SLF001
        repo._conn.commit()  # noqa: SLF001

    sql = """
        SELECT doc_id, kind, workspace_key, channel_id, anchor_ts, text, text_sha256
        FROM search_docs
        WHERE embedded_model IS NULL OR embedded_model != ?
        ORDER BY doc_id
    """
    params: list[Any] = [embedder.model_id]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = list(repo._conn.execute(sql, params).fetchall())  # noqa: SLF001
    embedded = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        texts = [r["text"] for r in batch]
        vectors = embedder.embed_documents(texts)
        now = _utc_now()
        for j, r in enumerate(batch):
            # Hash check + vector upsert + status update under one write lock so a
            # concurrent edit cannot leave a stale vector marked complete.
            with contextlib.suppress(sqlite3.OperationalError):
                # OperationalError if already inside a transaction.
                repo._conn.execute("BEGIN IMMEDIATE")  # noqa: SLF001
            try:
                cur = repo._conn.execute(  # noqa: SLF001
                    "SELECT text_sha256 FROM search_docs WHERE doc_id=?",
                    (int(r["doc_id"]),),
                ).fetchone()
                if not cur or cur["text_sha256"] != r["text_sha256"]:
                    repo._conn.execute("ROLLBACK")  # noqa: SLF001
                    continue
                ts_num = 0.0
                if r["anchor_ts"]:
                    try:
                        ts_num = float(r["anchor_ts"])
                    except ValueError:
                        ts_num = 0.0
                upsert_doc_embedding(
                    repo._conn,  # noqa: SLF001
                    doc_id=int(r["doc_id"]),
                    embedding=vectors[j],
                    workspace_key=r["workspace_key"],
                    channel_id=r["channel_id"],
                    ts_num=ts_num,
                    kind=r["kind"],
                    model_id=embedder.model_id,
                )
                updated = repo._conn.execute(  # noqa: SLF001
                    """
                    UPDATE search_docs
                    SET embedded_model=?, embedded_at=?
                    WHERE doc_id=? AND text_sha256=?
                    """,
                    (embedder.model_id, now, int(r["doc_id"]), r["text_sha256"]),
                )
                if updated.rowcount == 0:
                    # Roll back the vector upsert so a concurrent edit cannot leave
                    # a stale embedding marked complete for newer text.
                    repo._conn.execute("ROLLBACK")  # noqa: SLF001
                    continue
                repo._conn.execute("COMMIT")  # noqa: SLF001
                embedded += 1
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    repo._conn.execute("ROLLBACK")  # noqa: SLF001
                raise

    repo._conn.execute(  # noqa: SLF001
        """
        INSERT INTO meta(key, value) VALUES ('embedding_model', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (embedder.model_id,),
    )
    repo._conn.execute(  # noqa: SLF001
        """
        INSERT INTO meta(key, value) VALUES ('embedding_dim', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (str(embedder.dim),),
    )
    repo._conn.commit()  # noqa: SLF001
    return {"ok": True, "embedded": embedded, "model_id": embedder.model_id}
