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
from slack_cache_mcp.store.sqlite.vec import ensure_docs_vec, try_load_sqlite_vec
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
    assert try_load_sqlite_vec(archive._conn)  # noqa: SLF001
    assert ensure_docs_vec(archive._conn)  # noqa: SLF001


def test_hashing_embedder_deterministic() -> None:
    emb = HashingEmbedder(dim=64)
    a = emb.embed_query("hello widgets")
    b = emb.embed_documents(["hello widgets"])[0]
    assert a.shape == (64,)
    assert np.allclose(a, b)


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
    svc = SearchService(archive, embedder=emb)
    hits = svc.search_messages(query="widgets", mode="hybrid", limit=5)
    assert hits["mode"] == "hybrid"
    assert hits["results"]
    sem = svc.search_messages(query="widgets", mode="semantic", limit=5)
    assert sem["mode"] == "semantic"


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
