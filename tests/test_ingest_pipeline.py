"""End-to-end ingest on synthetic profile + idempotency + no xox."""

from __future__ import annotations

from pathlib import Path

from slack_cache_mcp.config import load_settings
from slack_cache_mcp.pipeline import run_snapshot_once
from slack_cache_mcp.store.sqlite.repo import SqliteRepository
from tests.fixture_gen.build_profile import build_synthetic_profile


def test_pipeline_ingest_and_idempotent(tmp_path: Path) -> None:
    profile = build_synthetic_profile(tmp_path / "profile")
    data = tmp_path / "data"
    settings = load_settings(data_dir=data, source=profile)
    r1 = run_snapshot_once(settings)
    assert r1["states_decoded"] == 3
    assert r1["messages_ingested"] > 0
    assert r1["token_leaks"] == []
    assert len(r1["workspaces"]) >= 3

    repo = SqliteRepository(settings.archive_db, read_only=True)
    before_versions = repo._conn.execute(  # noqa: SLF001
        "SELECT COUNT(*) AS c FROM message_versions"
    ).fetchone()["c"]
    before_msgs = repo._conn.execute(  # noqa: SLF001
        "SELECT COUNT(*) AS c FROM messages"
    ).fetchone()["c"]
    repo.close()

    r2 = run_snapshot_once(settings)
    # re-ingest may recount observations but version rows stay same count
    repo2 = SqliteRepository(settings.archive_db, read_only=True)
    after_versions = repo2._conn.execute(  # noqa: SLF001
        "SELECT COUNT(*) AS c FROM message_versions"
    ).fetchone()["c"]
    after_msgs = repo2._conn.execute(  # noqa: SLF001
        "SELECT COUNT(*) AS c FROM messages"
    ).fetchone()["c"]
    assert after_versions == before_versions
    assert after_msgs == before_msgs
    assert repo2.scan_for_token_leak() == []

    # tombstone present
    tomb = repo2._conn.execute(  # noqa: SLF001
        "SELECT deleted_state FROM messages WHERE subtype='tombstone' LIMIT 1"
    ).fetchone()
    assert tomb is not None
    assert tomb["deleted_state"] == "deleted"

    # search finds synthetic text
    from slack_cache_mcp.search.service import SearchService

    hits = SearchService(repo2).search_messages(query="widgets", mode="lexical", limit=5)
    assert hits["results"]
    repo2.close()
    assert r2["states_decoded"] == 3


def test_content_hash_ignores_client_private() -> None:
    from slack_cache_mcp.ingest.hashing import content_sha256

    a = {
        "ts": "1.0",
        "text": "hi",
        "user": "U1",
        "__meta__": {"x": 1},
        "_hidden_reply": True,
        "reply_count": 3,
        "reactions": [{"name": "thumbsup"}],
    }
    b = {
        "ts": "1.0",
        "text": "hi",
        "user": "U1",
        "__meta__": {"x": 2},
        "reply_count": 99,
        "reactions": [],
    }
    assert content_sha256(a) == content_sha256(b)
