"""Background / on-demand embedding of pending search_docs."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from slack_cache_mcp.store.sqlite.vec import ensure_docs_vec, upsert_doc_embedding

if TYPE_CHECKING:
    from slack_cache_mcp.search.embedder import Embedder
    from slack_cache_mcp.store.sqlite.repo import SqliteRepository

_EMBED_OP_LOCK_META_KEY = "embedding_op_lock"


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _is_sqlite_busy(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _renew_meta_lease_on_side_conn(
    db_path: str,
    owner: str,
    *,
    lease_s: float = 120.0,
    busy_deadline: float | None = None,
    stop: threading.Event | None = None,
    commit_gate: threading.Lock | None = None,
    commit_allowed: dict[str, bool] | None = None,
) -> bool | None:
    """Renew meta lease via a private connection (safe for heartbeat threads).

    Returns True on success, False on ownership loss / hard failure, or None
    when ``stop`` is set (cancelled) or commit is disallowed. Transient SQLite
    busy / locked errors are retried until ``busy_deadline`` (default: now +
    lease_s); retries are cancellable. ``commit_gate`` + ``commit_allowed``
    serialize the final commit against teardown so a stalled renew cannot
    extend a lease after ownership release begins.
    """
    deadline = (
        float(busy_deadline)
        if busy_deadline is not None
        else time.time() + max(lease_s, 1.0)
    )
    while True:
        if stop is not None and stop.is_set():
            return None
        if commit_allowed is not None and not commit_allowed.get("ok", False):
            return None
        conn: sqlite3.Connection | None = None
        try:
            # Short per-attempt timeout; we retry busy ourselves until deadline.
            conn = sqlite3.connect(db_path, timeout=1.0)
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT value FROM meta WHERE key=?", (_EMBED_OP_LOCK_META_KEY,)
            ).fetchone()
            if not row:
                conn.rollback()
                return False
            try:
                data = json.loads(str(row["value"]))
            except (json.JSONDecodeError, TypeError):
                conn.rollback()
                return False
            if not isinstance(data, dict) or str(data.get("owner")) != owner:
                conn.rollback()
                return False
            payload = json.dumps(
                {
                    "owner": owner,
                    "pid": os.getpid(),
                    "expires_at": time.time() + max(lease_s, 1.0),
                    "protocol": "file+meta",
                }
            )
            conn.execute(
                """
                INSERT INTO meta(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (_EMBED_OP_LOCK_META_KEY, payload),
            )
            # Commit only while teardown still allows it (mutex with join path).
            if commit_gate is not None and commit_allowed is not None:
                with commit_gate:
                    if not commit_allowed.get("ok", False) or (
                        stop is not None and stop.is_set()
                    ):
                        conn.rollback()
                        return None
                    conn.commit()
                    return True
            if stop is not None and stop.is_set():
                conn.rollback()
                return None
            conn.commit()
            return True
        except Exception as exc:
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.rollback()
            if stop is not None and stop.is_set():
                return None
            if commit_allowed is not None and not commit_allowed.get("ok", False):
                return None
            if _is_sqlite_busy(exc) and time.time() < deadline:
                # Interruptible backoff so inference can join before lock release.
                if stop is not None:
                    if stop.wait(0.05):
                        return None
                else:
                    time.sleep(0.05)
                continue
            return False
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()


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
        with contextlib.suppress(sqlite3.OperationalError):
            repo._conn.execute("BEGIN IMMEDIATE")  # noqa: SLF001
        try:
            if lock_owner is not None and not repo.renew_embedding_op_lock(lock_owner):
                repo._conn.execute("ROLLBACK")  # noqa: SLF001
                return {
                    "ok": False,
                    "embedded": 0,
                    "model_id": embedder.model_id,
                    "error": "embedding_op_lock_lost",
                }
            repo._conn.execute(  # noqa: SLF001
                "UPDATE search_docs SET embedded_model=NULL, embedded_at=NULL"
            )
            with contextlib.suppress(sqlite3.OperationalError):
                repo._conn.execute("DELETE FROM docs_vec")  # noqa: SLF001
            with contextlib.suppress(sqlite3.OperationalError):
                repo._conn.execute("DELETE FROM doc_embeddings")  # noqa: SLF001
            repo._conn.commit()  # noqa: SLF001
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                repo._conn.execute("ROLLBACK")  # noqa: SLF001
            raise

    # Reconcile only when counts diverge (or vec missing); fence inside the same
    # write txn so a stolen lease cannot drop/rebuild docs_vec under us.
    with contextlib.suppress(sqlite3.OperationalError):
        repo._conn.execute("BEGIN IMMEDIATE")  # noqa: SLF001
    try:
        if lock_owner is not None and not repo.renew_embedding_op_lock(lock_owner):
            repo._conn.execute("ROLLBACK")  # noqa: SLF001
            return {
                "ok": False,
                "embedded": 0,
                "model_id": embedder.model_id,
                "error": "embedding_op_lock_lost",
            }
        need_reconcile = True
        with contextlib.suppress(sqlite3.OperationalError):
            vec_n = int(
                repo._conn.execute(  # noqa: SLF001
                    "SELECT COUNT(*) AS c FROM docs_vec"
                ).fetchone()[0]
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
    except Exception:
        with contextlib.suppress(sqlite3.Error):
            repo._conn.execute("ROLLBACK")  # noqa: SLF001
        raise

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
    db_path = str(repo.path)
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        lost = _fence(embedded=embedded)
        if lost is not None:
            return lost
        texts = [r["text"] for r in batch]
        # Short renewable lease via a side connection during inference.
        stop_beat = threading.Event()
        done_beat = threading.Event()
        beat_ok = {"ok": True}
        commit_gate = threading.Lock()
        commit_allowed = {"ok": True}

        def _beat(
            _stop: threading.Event = stop_beat,
            _done: threading.Event = done_beat,
            _ok: dict[str, bool] = beat_ok,
            _owner: str | None = lock_owner,
            _path: str = db_path,
            _gate: threading.Lock = commit_gate,
            _allowed: dict[str, bool] = commit_allowed,
        ) -> None:
            # Last renew (or acquire) left ~120s; after a 30s sleep ~90s remain.
            # Busy retries must not outlive a still-valid lease window.
            try:
                lease_s = 120.0
                while not _stop.wait(30.0):
                    if _owner is None:
                        return
                    try:
                        ok = _renew_meta_lease_on_side_conn(
                            _path,
                            _owner,
                            lease_s=lease_s,
                            busy_deadline=time.time() + max(lease_s - 30.0, 1.0),
                            stop=_stop,
                            commit_gate=_gate,
                            commit_allowed=_allowed,
                        )
                    except Exception:
                        # Connect / unexpected errors: fail closed so inference aborts.
                        _ok["ok"] = False
                        return
                    if _stop.is_set() or ok is None:
                        # Cancelled by inference teardown — do not fail the batch.
                        return
                    if not ok:
                        _ok["ok"] = False
                        return
            finally:
                # Set only after side-conn renew returns (connection closed).
                _done.set()

        # Protect the whole started-thread lifetime (start → embed → teardown)
        # against Ctrl-C. Prefer done_beat over join(): CPython 3.12 can mark a
        # thread stopped after an interrupted join while it is still running.
        beater: threading.Thread | None = None
        interrupted = False
        thread_started = False
        embed_finished = False
        vectors: Any = None
        while True:
            try:
                if not thread_started:
                    if lock_owner is not None:
                        beater = threading.Thread(
                            target=_beat, name="embed-op-lock-beat", daemon=True
                        )
                        # Commit to teardown even if start() is interrupted.
                        thread_started = True
                        beater.start()
                    else:
                        done_beat.set()
                        thread_started = True

                if not embed_finished:
                    vectors = embedder.embed_documents(texts)
                    embed_finished = True

                stop_beat.set()
                with commit_gate:
                    commit_allowed["ok"] = False
                if beater is None and not done_beat.is_set():
                    done_beat.set()
                done_beat.wait()
                if beater is not None:
                    beater.join()
                break
            except KeyboardInterrupt:
                interrupted = True
                if not thread_started:
                    break
                # Skip re-embedding; retries only finish stop/gate/wait/join.
                embed_finished = True
                continue
        if interrupted:
            raise KeyboardInterrupt
        assert vectors is not None
        if not beat_ok["ok"]:
            return {
                "ok": False,
                "embedded": embedded,
                "model_id": embedder.model_id,
                "error": "embedding_op_lock_lost",
            }
        lost = _fence(embedded=embedded)
        if lost is not None:
            return lost
        now = _utc_now()
        for j, r in enumerate(batch):
            # Hash check + vector upsert + status update under one write lock so a
            # concurrent edit cannot leave a stale vector marked complete.
            with contextlib.suppress(sqlite3.OperationalError):
                # OperationalError if already inside a transaction.
                repo._conn.execute("BEGIN IMMEDIATE")  # noqa: SLF001
            try:
                if lock_owner is not None and not repo.renew_embedding_op_lock(
                    lock_owner
                ):
                    repo._conn.execute("ROLLBACK")  # noqa: SLF001
                    return {
                        "ok": False,
                        "embedded": embedded,
                        "model_id": embedder.model_id,
                        "error": "embedding_op_lock_lost",
                    }
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

    with contextlib.suppress(sqlite3.OperationalError):
        repo._conn.execute("BEGIN IMMEDIATE")  # noqa: SLF001
    try:
        if lock_owner is not None and not repo.renew_embedding_op_lock(lock_owner):
            repo._conn.execute("ROLLBACK")  # noqa: SLF001
            return {
                "ok": False,
                "embedded": embedded,
                "model_id": embedder.model_id,
                "error": "embedding_op_lock_lost",
            }
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
    except Exception:
        with contextlib.suppress(sqlite3.Error):
            repo._conn.execute("ROLLBACK")  # noqa: SLF001
        raise
    return {"ok": True, "embedded": embedded, "model_id": embedder.model_id}
