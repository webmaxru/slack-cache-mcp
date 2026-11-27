"""M4 hybrid search, embeddings, eval harness (offline)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from slack_cache_mcp.config import load_settings
from slack_cache_mcp.pipeline import run_snapshot_once
from slack_cache_mcp.search.docs import SearchDocsBuilder
from slack_cache_mcp.search.embedder import HashingEmbedder, rrf_score
from slack_cache_mcp.search.eval_harness import bench_hybrid_latency, run_eval
from slack_cache_mcp.search.hybrid import collapse_threads, fuse_rrf
from slack_cache_mcp.search.service import SearchService
from slack_cache_mcp.search.worker import embed_pending
from slack_cache_mcp.store.sqlite.repo import SCHEMA_VERSION, SqliteRepository
from slack_cache_mcp.store.sqlite.vec import try_load_sqlite_vec
from tests.fixture_gen.build_profile import build_synthetic_profile


@pytest.fixture()
def archive(tmp_path: Path) -> SqliteRepository:
    profile = build_synthetic_profile(tmp_path / "profile")
    settings = load_settings(data_dir=tmp_path / "data", source=profile)
    run_snapshot_once(settings)
    return SqliteRepository(settings.archive_db, read_only=False)


def test_schema_v4_search_docs(archive: SqliteRepository) -> None:
    ver = archive._conn.execute(  # noqa: SLF001
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()["value"]
    assert ver == SCHEMA_VERSION == "4"
    tables = {
        r[0]
        for r in archive._conn.execute(  # noqa: SLF001
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "search_docs" in tables
    assert "doc_embeddings" in tables
    # sqlite-vec may be unavailable (macOS Python without load_extension).
    _ = try_load_sqlite_vec(archive._conn)  # noqa: SLF001


def test_hashing_embedder_deterministic() -> None:
    emb = HashingEmbedder(dim=64)
    a = emb.embed_query("hello widgets")
    b = emb.embed_documents(["hello widgets"])[0]
    assert a.shape == (64,)
    assert np.allclose(a, b)


def test_hashing_embedder_unicode() -> None:
    emb = HashingEmbedder(dim=64)
    v = emb.embed_query("東京 привет")
    assert float(np.linalg.norm(v)) > 0.1


def test_rrf_and_collapse() -> None:
    assert rrf_score(1) > rrf_score(2)
    lex = [{"channel_id": "C1", "ts": "1.0", "score": 1.0, "text": "a"}]
    vec = [
        {"channel_id": "C1", "ts": "1.0", "score": 0.9, "text": "a"},
        {"channel_id": "C2", "ts": "2.0", "score": 0.8, "text": "b"},
    ]
    fused = fuse_rrf(lex, vec)
    assert fused[0]["channel_id"] == "C1"
    assert fused[0]["match"]["lexical_rank"] == 1
    collapsed = collapse_threads(
        [
            {"channel_id": "C1", "ts": "1.0", "thread_ts": "1.0", "score": 1.0},
            {"channel_id": "C1", "ts": "1.1", "thread_ts": "1.0", "score": 0.5},
        ]
    )
    assert len(collapsed) == 1
    assert collapsed[0]["hit_count"] == 2


def test_hybrid_search_after_embed(archive: SqliteRepository) -> None:
    emb = HashingEmbedder()
    counts = SearchDocsBuilder(archive).rebuild_all()
    assert counts["message"] > 0
    result = embed_pending(archive, emb)
    assert result["ok"]
    assert result["embedded"] > 0
    # Portable store always populated even without sqlite-vec.
    n = archive._conn.execute(  # noqa: SLF001
        "SELECT COUNT(*) AS c FROM doc_embeddings"
    ).fetchone()["c"]
    assert n > 0
    svc = SearchService(archive, embedder=emb)
    hits = svc.search_messages(query="widgets", mode="hybrid", limit=5)
    assert hits["mode"] == "hybrid"
    assert hits["results"]
    sem = svc.search_messages(query="widgets", mode="semantic", limit=5)
    assert sem["mode"] == "semantic"
    assert sem["results"]


def test_list_threads_and_whats_new(archive: SqliteRepository) -> None:
    threads = archive.list_threads(limit=20)
    assert isinstance(threads, list)
    wn = archive.whats_new(since="1970-01-01T00:00:00Z", limit=20)
    assert "messages" in wn
    assert wn["messages"]


def test_eval_hybrid_beats_lexical(tmp_path: Path) -> None:
    repo = SqliteRepository(tmp_path / "eval.db")
    metrics = run_eval(repo)
    assert metrics["queries"] >= 5
    assert metrics["pass_hybrid_ge_lexical_plus_10pct"] is True
    repo.close()


def test_bench_p95_small(tmp_path: Path) -> None:
    repo = SqliteRepository(tmp_path / "bench.db")
    result = bench_hybrid_latency(repo, n_messages=800, n_queries=20, embed_limit=800)
    assert result.get("ok") is True
    assert result["pass_p95_under_300ms"] is True
    repo.close()


def test_embeddings_offline_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("SLACKCACHE_EMBEDDER", "hashing")
    from slack_cache_mcp.embeddings.factory import load_embedder

    emb = load_embedder(models_dir=tmp_path / "models", force_hashing=True)
    assert emb.model_id.startswith("hashing")
    vec = emb.embed_query("ping")
    assert vec.shape[0] == emb.dim


def test_load_embedder_exact_archive_model(tmp_path: Path) -> None:
    from slack_cache_mcp.embeddings.factory import load_embedder
    from slack_cache_mcp.search.embedder import DEFAULT_MODEL_ID

    emb = load_embedder(models_dir=tmp_path / "models", model_id=DEFAULT_MODEL_ID)
    assert emb.model_id == DEFAULT_MODEL_ID
    with pytest.raises(ValueError, match="unsupported hashing"):
        load_embedder(models_dir=tmp_path / "models", model_id="hashing-trigram-v1")


def test_dirty_window_generation_ack(archive: SqliteRepository) -> None:
    archive.mark_windows_dirty("C_TEST")
    pending = dict(archive.peek_dirty_window_channels())
    g1 = pending["C_TEST"]
    archive.mark_windows_dirty("C_TEST")
    pending = dict(archive.peek_dirty_window_channels())
    g2 = pending["C_TEST"]
    assert g2 > g1
    archive.ack_dirty_window_channel("C_TEST", g1)
    pending = dict(archive.peek_dirty_window_channels())
    assert pending["C_TEST"] == g2
    archive.ack_dirty_window_channel("C_TEST", g2)
    assert "C_TEST" not in dict(archive.peek_dirty_window_channels())
    # Generations remain monotonic across acknowledgements.
    archive.mark_windows_dirty("C_TEST")
    pending = dict(archive.peek_dirty_window_channels())
    assert pending["C_TEST"] > g2
    # Stale ack of an old generation must not clear newer work.
    archive.ack_dirty_window_channel("C_TEST", g1)
    assert "C_TEST" in dict(archive.peek_dirty_window_channels())


def test_dirty_generation_upgrade_no_collision(archive: SqliteRepository) -> None:
    """Pre-seq dirty map gens must not collide with a fresh sequence key."""
    # Simulate upgrade: dirty map has gen 1, sequence meta absent.
    archive._conn.execute(  # noqa: SLF001
        """
        INSERT INTO meta(key, value) VALUES ('window_dirty_channels', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        ('{"C_OLD": 1}',),
    )
    archive._conn.execute(  # noqa: SLF001
        "DELETE FROM meta WHERE key='window_dirty_seq'"
    )
    archive._conn.commit()  # noqa: SLF001
    # Peek seeds the sequence before any ack can race a new mark.
    peeked = dict(archive.peek_dirty_window_channels())
    assert peeked["C_OLD"] == 1
    archive.ack_dirty_window_channel("C_OLD", 1)
    archive.mark_windows_dirty("C_NEW")
    pending = dict(archive.peek_dirty_window_channels())
    assert pending["C_NEW"] > 1
    # Stale ack of gen 1 must not clear the new channel's work.
    archive.ack_dirty_window_channel("C_NEW", 1)
    assert "C_NEW" in dict(archive.peek_dirty_window_channels())


def test_refuse_hashing_demotion(archive: SqliteRepository) -> None:
    from slack_cache_mcp.search.embedder import HashingEmbedder
    from slack_cache_mcp.search.worker import embed_pending

    archive._conn.execute(  # noqa: SLF001
        """
        INSERT INTO meta(key, value) VALUES ('embedding_model', 'onnx-prod-model')
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """
    )
    archive._conn.commit()  # noqa: SLF001
    result = embed_pending(archive, HashingEmbedder(), limit=10)
    assert result["ok"] is False
    assert result.get("error") == "refuse_hashing_demotion"
    # Explicit rebuild may allow the demotion.
    allowed = embed_pending(
        archive, HashingEmbedder(), limit=10, allow_hashing_demotion=True
    )
    assert allowed.get("error") != "refuse_hashing_demotion"


def test_embedding_op_lock_exclusive(archive: SqliteRepository) -> None:
    """Leased lock survives commits, blocks a second owner, and expires."""
    import json
    import time

    assert archive.acquire_embedding_op_lock("owner-a", lease_s=60.0) is True
    archive._conn.commit()  # noqa: SLF001
    assert archive.embedding_op_lock_held() is True
    assert archive.embedding_op_lock_owned("owner-a") is True
    assert archive.acquire_embedding_op_lock("owner-b") is False
    archive.release_embedding_op_lock("owner-b")  # no-op for other owner
    assert archive.embedding_op_lock_held() is True
    archive.release_embedding_op_lock("owner-a")
    assert archive.embedding_op_lock_held() is False
    assert archive.acquire_embedding_op_lock("owner-b") is True
    archive.release_embedding_op_lock("owner-b")
    # Expired lease is stealable (crash recovery).
    archive._conn.execute(  # noqa: SLF001
        """
        INSERT INTO meta(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (
            archive._EMBED_OP_LOCK_KEY,  # noqa: SLF001
            json.dumps({"owner": "dead", "expires_at": time.time() - 10}),
        ),
    )
    archive._conn.commit()  # noqa: SLF001
    assert archive.embedding_op_lock_held() is False
    assert archive.acquire_embedding_op_lock("owner-c") is True
    archive.release_embedding_op_lock("owner-c")


def test_whats_new_legacy_rowbound(archive: SqliteRepository) -> None:
    """Legacy finish pins to insertion boundary, not max first_seen rowid."""
    store = Path(archive.path).parent / "client_state.json"
    first = archive.whats_new(since="1970-01-01T00:00:00Z", limit=1, watermark_store=store)
    assert first["has_more"] or first["watermark"].startswith("rowid:")
    if first["has_more"]:
        assert "#rowbound:" in first["watermark"]
        # Drain legacy pages
        mark = first["watermark"]
        for _ in range(50):
            page = archive.whats_new(since=mark, limit=5, watermark_store=store)
            mark = page["watermark"]
            if not page["has_more"]:
                break
        assert mark.startswith("rowid:")


def test_new_reply_no_phantom_thread_doc(archive: SqliteRepository) -> None:
    from slack_cache_mcp.search.docs import SearchDocsBuilder

    builder = SearchDocsBuilder(archive)
    # Pick an existing root and simulate a brand-new reply refresh.
    root = archive._conn.execute(  # noqa: SLF001
        """
        SELECT channel_id, ts FROM messages
        WHERE deleted_state='none' AND (thread_ts IS NULL OR thread_ts=ts)
        LIMIT 1
        """
    ).fetchone()
    assert root is not None
    reply_ts = f"{float(root['ts']) + 0.001:.6f}"
    builder.refresh_aggregates(
        root["channel_id"],
        root["ts"],
        message_ts=reply_ts,
        text_changed=True,
        is_new=True,
    )
    phantom = archive._conn.execute(  # noqa: SLF001
        """
        SELECT 1 FROM search_docs
        WHERE kind='thread' AND channel_id=? AND anchor_ts=?
        """,
        (root["channel_id"], reply_ts),
    ).fetchone()
    assert phantom is None
