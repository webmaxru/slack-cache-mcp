"""Search filter / operator / date / multi-id coverage."""

from __future__ import annotations

from pathlib import Path

import pytest

from slack_cache_mcp.config import load_settings
from slack_cache_mcp.pipeline import run_snapshot_once
from slack_cache_mcp.search.service import SearchService
from slack_cache_mcp.store.sqlite.repo import SqliteRepository
from tests.fixture_gen.build_profile import build_synthetic_profile


@pytest.fixture()
def repo(tmp_path: Path) -> SqliteRepository:
    profile = build_synthetic_profile(tmp_path / "profile")
    settings = load_settings(data_dir=tmp_path / "data", source=profile)
    run_snapshot_once(settings)
    return SqliteRepository(settings.archive_db, read_only=True)


def test_filter_only_in_channel(repo: SqliteRepository) -> None:
    svc = SearchService(repo)
    hits = svc.search_messages(query="in:#general", mode="lexical", limit=20)
    assert hits["mode"] == "filter"
    assert hits["results"]
    assert all(r["channel_id"] for r in hits["results"])


def test_unknown_from_raises(repo: SqliteRepository) -> None:
    svc = SearchService(repo)
    with pytest.raises(ValueError, match="Unknown user"):
        svc.search_messages(query="from:@no_such_user_xyz budget", mode="lexical")


def test_after_date_excludes_old(repo: SqliteRepository) -> None:
    svc = SearchService(repo)
    # Far-future bound → empty; past bound → some hits for widgets
    empty = svc.search_messages(query="widgets after:2099-01-01", mode="lexical", limit=10)
    assert empty["results"] == []
    some = svc.search_messages(query="widgets after:2020-01-01", mode="lexical", limit=10)
    assert some["results"]


def test_multi_channel_ids(repo: SqliteRepository) -> None:
    svc = SearchService(repo)
    # Collect two channel ids from archive
    rows = repo._conn.execute(  # noqa: SLF001
        "SELECT DISTINCT channel_id FROM messages LIMIT 2"
    ).fetchall()
    if len(rows) < 2:
        pytest.skip("need >=2 channels")
    ids = [r["channel_id"] for r in rows]
    hits = svc.search_messages(query="", mode="lexical", channels=ids, limit=50)
    assert hits["results"]
    assert {h["channel_id"] for h in hits["results"]} <= set(ids)


def test_exact_mode_honors_workspace_filter(repo: SqliteRepository) -> None:
    svc = SearchService(repo)
    wss = [w["key"] for w in repo.list_workspaces()]
    assert wss
    hits = svc.search_messages(
        query="widgets", mode="exact", workspace=wss[0], limit=20
    )
    for h in hits["results"]:
        assert h["workspace_key"] == wss[0]


def test_unsupported_operator_rejected(repo: SqliteRepository) -> None:
    svc = SearchService(repo)
    with pytest.raises(ValueError, match="Unsupported"):
        svc.search_messages(query="widgets with:@alice", mode="lexical")


def test_is_deleted_returns_tombstones(repo: SqliteRepository) -> None:
    svc = SearchService(repo)
    hits = svc.search_messages(query="is:deleted", mode="lexical", limit=20)
    assert hits["results"]
    assert all(r["deleted_state"] != "none" for r in hits["results"])
