"""Background / on-demand embedding of pending search_docs."""

from __future__ import annotations

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
    """Embed search_docs rows with ``embedded_model IS NULL``."""
    if not ensure_docs_vec(repo._conn, dim=embedder.dim):  # noqa: SLF001
        return {"ok": False, "error": "sqlite-vec unavailable", "embedded": 0}

    sql = """
        SELECT doc_id, kind, workspace_key, channel_id, anchor_ts, text
        FROM search_docs
        WHERE embedded_model IS NULL
        ORDER BY doc_id
    """
    params: list[Any] = []
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
            ts_num = None
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
            )
            repo._conn.execute(  # noqa: SLF001
                """
                UPDATE search_docs
                SET embedded_model=?, embedded_at=?
                WHERE doc_id=?
                """,
                (embedder.model_id, now, int(r["doc_id"])),
            )
            embedded += 1
        repo._conn.commit()  # noqa: SLF001

    repo._conn.execute(  # noqa: SLF001
        """
        INSERT INTO meta(key, value) VALUES ('embedding_model', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (embedder.model_id,),
    )
    repo._conn.commit()  # noqa: SLF001
    return {"ok": True, "embedded": embedded, "model_id": embedder.model_id}
