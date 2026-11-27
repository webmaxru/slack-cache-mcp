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
    allow_hashing_demotion: bool = False,
    lock_owner: str | None = None,
) -> dict[str, Any]:
    """Embed search_docs rows pending for the active model.

    ``allow_hashing_demotion`` must be True for an intentional rebuild that
    switches a persisted ONNX index to hashing (e.g. CLI ``rebuild --embeddings``
    with ``SLACKCACHE_EMBEDDER=hashing``). Daemon auto-fallback leaves it False.

    When ``lock_owner`` is set, fence the crash-released OS lock before every
    mutating transaction (model wipe, per-batch upserts, final meta write).
    """
    def _fence(*, embedded: int = 0) -> dict[str, Any] | None:
        if lock_owner is None:
            return None
        if not repo.renew_embedding_op_lock(lock_owner):
            return {
                "ok": False,
                "embedded": embedded,
                "model_id": embedder.model_id,
                "error": "embedding_op_lock_lost",
            }
        return None

    lost = _fence()
    if lost is not None:
        return lost
    prev = repo._conn.execute(  # noqa: SLF001
        "SELECT value FROM meta WHERE key='embedding_model'"
    ).fetchone()
    prev_dim = repo._conn.execute(  # noqa: SLF001
        "SELECT value FROM meta WHERE key='embedding_dim'"
    ).fetchone()
    # Never auto-demote a persisted non-hashing index to hashing (daemon
    # restart / temporary ONNX load failure would wipe the production index).
    if (
        not allow_hashing_demotion
        and prev
        and not str(prev["value"]).startswith("hashing")
        and str(embedder.model_id).startswith("hashing")
    ):
        return {
            "ok": False,
            "embedded": 0,
            "model_id": embedder.model_id,
            "error": "refuse_hashing_demotion",
            "persisted_model": str(prev["value"]),
        }
    if (prev and prev["value"] != embedder.model_id) or (
        prev_dim and int(prev_dim["value"]) != int(embedder.dim)
    ):
        lost = _fence()
        if lost is not None:
            return lost
        repo._conn.execute(  # noqa: SLF001
            "UPDATE search_docs SET embedded_model=NULL, embedded_at=NULL"
        )
        with contextlib.suppress(sqlite3.OperationalError):
            repo._conn.execute("DELETE FROM docs_vec")  # noqa: SLF001
        with contextlib.suppress(sqlite3.OperationalError):
            repo._conn.execute("DELETE FROM doc_embeddings")  # noqa: SLF001
        repo._conn.commit()  # noqa: SLF001

    # Reconcile only when counts diverge (or vec missing); commit before inference.
    need_reconcile = True
    with contextlib.suppress(sqlite3.OperationalError):
        vec_n = int(
            repo._conn.execute("SELECT COUNT(*) AS c FROM docs_vec").fetchone()[0]  # noqa: SLF001
        )
        blob_n = int(
            repo._conn.execute(  # noqa: SLF001
                "SELECT COUNT(*) AS c FROM doc_embeddings"
            ).fetchone()[0]
        )
        need_reconcile = vec_n != blob_n
    ensure_docs_vec(  # noqa: SLF001
        repo._conn, dim=embedder.dim, reconcile=need_reconcile
    )
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
        lost = _fence(embedded=embedded)
        if lost is not None:
            return lost
        texts = [r["text"] for r in batch]
        vectors = embedder.embed_documents(texts)
        now = _utc_now()
        for j, r in enumerate(batch):
            lost = _fence(embedded=embedded)
            if lost is not None:
                return lost
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

    lost = _fence(embedded=embedded)
    if lost is not None:
        return lost
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
