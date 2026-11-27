"""M5.1: Edge Chromium Slack IndexedDB → same archive.db with dedupe."""

from __future__ import annotations

from pathlib import Path

from slack_cache_mcp.config import Settings, load_settings
from slack_cache_mcp.desktop.locate import discover_profiles
from slack_cache_mcp.desktop.snapshot import ALLOWLIST_EDGE_IDB, snapshot_profile
from slack_cache_mcp.pipeline import run_snapshot_once
from slack_cache_mcp.store.sqlite.repo import SqliteRepository
from tests.fixture_gen.build_profile import (
    build_desktop_missing_reply_profile,
    build_edge_browser_profile,
    build_synthetic_profile,
)


def test_locate_windows_edge_when_slack_idb_present(tmp_path: Path) -> None:
    local = tmp_path / "Local"
    roaming = tmp_path / "Roaming"
    edge = local / "Microsoft" / "Edge" / "User Data" / "Default"
    build_edge_browser_profile(edge)
    # Chrome with Slack IDB must stay out of scope for M5.1.
    chrome = local / "Google" / "Chrome" / "User Data" / "Default"
    build_edge_browser_profile(chrome)
    env = {"LOCALAPPDATA": str(local), "APPDATA": str(roaming)}
    found = discover_profiles(env=env, platform="win32")
    kinds = {p.kind for p in found if p.valid}
    assert "win-edge" in kinds
    assert all(p.kind != "win-chrome" for p in found)
    assert all("Chrome" not in str(p.path) for p in found)


def test_locate_skips_edge_without_slack_idb(tmp_path: Path) -> None:
    local = tmp_path / "Local"
    roaming = tmp_path / "Roaming"
    edge = local / "Microsoft" / "Edge" / "User Data" / "Default"
    edge.mkdir(parents=True)
    (edge / "Cookies").write_text("nope", encoding="utf-8")
    env = {"LOCALAPPDATA": str(local), "APPDATA": str(roaming)}
    found = discover_profiles(env=env, platform="win32")
    assert all(p.kind != "win-edge" for p in found)


def test_override_path_infers_win_edge(tmp_path: Path) -> None:
    edge = tmp_path / "Microsoft" / "Edge" / "User Data" / "Default"
    build_edge_browser_profile(edge)
    found = discover_profiles(overrides=[edge])
    assert len(found) == 1
    assert found[0].kind == "win-edge"
    assert found[0].valid
    assert found[0].is_edge_browser


def test_edge_snapshot_copies_only_slack_idb(tmp_path: Path) -> None:
    src = build_edge_browser_profile(tmp_path / "Edge" / "User Data" / "Default")
    mirror = tmp_path / "mirror"
    stats = snapshot_profile(src, mirror, require_stable_blobs=False, allowlist=ALLOWLIST_EDGE_IDB)
    assert stats.copied > 0
    assert (mirror / "IndexedDB" / "https_app.slack.com_0.indexeddb.leveldb" / "CURRENT").is_file()
    assert (
        mirror / "IndexedDB" / "https_app.slack.com_0.indexeddb.blob" / "1" / "00" / "1"
    ).is_file()
    # Forbidden / out-of-allowlist paths must be absent from the mirror.
    assert not (mirror / "Cookies").exists()
    assert not (mirror / "Local State").exists()
    assert not (mirror / "Network").exists()
    assert not (mirror / "Login Data").exists()
    assert not (mirror / "Local Storage").exists()
    assert not (mirror / "IndexedDB" / "https_other.example_0.indexeddb.leveldb").exists()
    assert "LOCK" not in {p.name for p in (mirror / "IndexedDB").rglob("*") if p.is_file()}


def test_edge_and_desktop_dedupe_into_same_archive(tmp_path: Path) -> None:
    desktop = build_desktop_missing_reply_profile(tmp_path / "SlackDesktop")
    edge = build_edge_browser_profile(
        tmp_path / "Microsoft" / "Edge" / "User Data" / "Default",
        with_reply_body=True,
    )
    data = tmp_path / "data"
    settings = Settings(data_dir=data, source_overrides=[desktop, edge])
    result = run_snapshot_once(settings)
    assert "error" not in result
    assert result["messages_ingested"] > 0
    assert result["token_leaks"] == []

    kinds = {s["kind"] for s in result["sources"]}
    assert "win-edge" in kinds
    assert "override" in kinds  # plain desktop override path
    assert len({s["source_id"] for s in result["sources"]}) == 2

    repo = SqliteRepository(settings.archive_db, read_only=True)
    # Overlapping root collapses to one current message row.
    roots = repo._conn.execute(  # noqa: SLF001
        """
        SELECT COUNT(*) AS c FROM messages
        WHERE channel_id='C0SYNTHPUB1' AND ts='1763400000.000100'
        """
    ).fetchone()["c"]
    assert roots == 1

    # Edge filled the reply body desktop lacked.
    reply = repo._conn.execute(  # noqa: SLF001
        """
        SELECT text, search_text FROM messages
        WHERE channel_id='C0SYNTHPUB1' AND ts='1763400001.000200'
        """
    ).fetchone()
    assert reply is not None
    assert "agreeing" in reply["text"]

    # Edge-only extra reply is present.
    edge_only = repo._conn.execute(  # noqa: SLF001
        """
        SELECT text FROM messages
        WHERE channel_id='C0SYNTHPUB1' AND ts='1763400999.000100'
        """
    ).fetchone()
    assert edge_only is not None
    assert "edge-only" in edge_only["text"]

    # Distinct sighting labels for desktop vs edge on the shared root.
    sights = repo._conn.execute(  # noqa: SLF001
        """
        SELECT DISTINCT source FROM message_sightings
        WHERE channel_id='C0SYNTHPUB1' AND ts='1763400000.000100'
        """
    ).fetchall()
    sources = {r["source"] for r in sights}
    assert any(s.startswith("edge.") for s in sources)
    assert any(not s.startswith("edge.") for s in sources)

    status = repo.cache_status()
    assert any(s["kind"] == "win-edge" for s in status["sources"])
    assert repo.scan_for_token_leak() == []
    repo.close()

    # Edge mirror must not retain credential bait.
    edge_src_id = next(s["source_id"] for s in result["sources"] if s["kind"] == "win-edge")
    edge_mirror = settings.mirror_dir / edge_src_id
    assert not (edge_mirror / "Cookies").exists()
    assert not (edge_mirror / "Local Storage").exists()
    assert not (edge_mirror / "Local State").exists()


def test_full_synthetic_desktop_still_works(tmp_path: Path) -> None:
    """Regression: standard desktop fixture ingest unchanged."""
    profile = build_synthetic_profile(tmp_path / "profile")
    settings = load_settings(data_dir=tmp_path / "data", source=profile)
    r = run_snapshot_once(settings)
    assert r["states_decoded"] == 3
    assert r["messages_ingested"] > 0
