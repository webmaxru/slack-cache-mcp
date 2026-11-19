"""MCP server tool smoke test (in-process, sync)."""

from __future__ import annotations

from pathlib import Path

from slack_cache_mcp.config import load_settings
from slack_cache_mcp.mcp_server.server import build_server
from slack_cache_mcp.pipeline import run_snapshot_once
from slack_cache_mcp.search.service import SearchService
from slack_cache_mcp.store.sqlite.repo import SqliteRepository
from tests.fixture_gen.build_profile import build_synthetic_profile


def test_mcp_server_builds_and_archive_tools_work(tmp_path: Path) -> None:
    profile = build_synthetic_profile(tmp_path / "profile")
    data = tmp_path / "data"
    settings = load_settings(data_dir=data, source=profile)
    run_snapshot_once(settings)

    server = build_server(settings)
    assert server is not None

    repo = SqliteRepository(settings.archive_db, read_only=True)
    assert repo.list_workspaces()
    assert repo.list_channels()
    assert repo.cache_status()
    hits = SearchService(repo).search_messages(query="synthetic", mode="lexical")
    assert "results" in hits
    assert repo.resolve("general", kind="channel")
    assert repo.get_user("selfuser")
