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

    # Edge mirror must not retain credential bait or raw IndexedDB (token-bearing Redux).
    edge_src_id = next(s["source_id"] for s in result["sources"] if s["kind"] == "win-edge")
    edge_mirror = settings.mirror_dir / edge_src_id
    assert not (edge_mirror / "Cookies").exists()
    assert not (edge_mirror / "Local Storage").exists()
    assert not (edge_mirror / "Local State").exists()
    assert not (edge_mirror / "IndexedDB").exists()


def test_edge_override_without_idb_does_not_use_desktop_allowlist(tmp_path: Path) -> None:
    """Invalid Edge path must not fall back to desktop Local Storage copy."""
    edge = tmp_path / "Microsoft" / "Edge" / "User Data" / "Default"
    edge.mkdir(parents=True)
    (edge / "Cookies").write_text("secret", encoding="utf-8")
    ls = edge / "Local Storage" / "leveldb"
    ls.mkdir(parents=True)
    (ls / "CURRENT").write_text("x", encoding="utf-8")
    (edge / "Local Storage" / "localConfig_v2.json").write_text(
        '{"token":"xoxc-FAKE"}', encoding="utf-8"
    )
    settings = Settings(data_dir=tmp_path / "data", source_overrides=[edge])
    result = run_snapshot_once(settings)
    assert result.get("error") == "no_valid_profiles"
    assert not (settings.mirror_dir).exists() or not any(settings.mirror_dir.iterdir())


def test_second_source_records_sightings_on_known_generation(tmp_path: Path) -> None:
    """Identical sanitized state from Edge after desktop still adds edge.* sightings."""
    import shutil

    edge = build_edge_browser_profile(
        tmp_path / "Microsoft" / "Edge" / "User Data" / "Default",
        with_reply_body=True,
    )
    # Desktop-shaped tree sharing the exact same IndexedDB blobs → same state sha256.
    desktop = tmp_path / "SlackDesktop"
    shutil.copytree(edge / "IndexedDB", desktop / "IndexedDB")
    (desktop / "storage").mkdir(parents=True)
    (desktop / "storage" / "root-state.json").write_text("{}", encoding="utf-8")
    (desktop / "local-settings.json").write_text("{}", encoding="utf-8")

    data = tmp_path / "data"
    r1 = run_snapshot_once(Settings(data_dir=data, source_overrides=[desktop]))
    assert r1["generations_new"] >= 1
    r2 = run_snapshot_once(Settings(data_dir=data, source_overrides=[edge]))
    assert r2["skipped_known_generations"] >= 1
    repo = SqliteRepository(data / "archive.db", read_only=True)
    sights = repo._conn.execute(  # noqa: SLF001
        """
        SELECT DISTINCT source FROM message_sightings
        WHERE channel_id='C0SYNTHPUB1' AND ts='1763400000.000100'
        """
    ).fetchall()
    sources = {r["source"] for r in sights}
    assert any(s.startswith("edge.") for s in sources)
    assert any(not s.startswith("edge.") for s in sources)
    repo.close()


def test_desktop_does_not_suspect_edge_only_replies(tmp_path: Path) -> None:
    """Thinner desktop window must not mark Edge-filled replies as suspected."""
    from tests.fixture_gen.build_profile import _workspace_state, write_blob

    edge = build_edge_browser_profile(
        tmp_path / "Microsoft" / "Edge" / "User Data" / "Default",
        with_reply_body=True,
    )
    desktop = build_desktop_missing_reply_profile(tmp_path / "SlackDesktop")
    data = tmp_path / "data"
    run_snapshot_once(Settings(data_dir=data, source_overrides=[edge]))

    # Two distinct desktop generations lacking the Edge-only reply (streak threshold=2).
    for blob_n in (2, 3):
        state = _workspace_state(
            workspace_key="T0SYNTHAAA",
            team_id="T0SYNTHAAA",
            user_id="U0SELF0001",
            team_name="Synth Team A",
            domain="synth-a",
            guest=False,
        )
        pub = state["messages"]["C0SYNTHPUB1"]
        pub.pop("1763400001.000200", None)
        # Vary content so each pass is a new state generation.
        pub["1763400000.000100"]["text"] = f"synthetic root gen{blob_n} about widgets"
        pub["1763400000.000100"]["reply_count"] = 1
        pub["1763400000.000100"]["latest_reply"] = "1763400001.000200"
        for slice_name in ("threads", "allThreads"):
            bucket = state.get(slice_name)
            if isinstance(bucket, dict) and "C0SYNTHPUB1" in bucket:
                bucket["C0SYNTHPUB1"] = {
                    "1763400000.000100": {
                        "replies": [pub["1763400000.000100"]],
                        "reply_count": 1,
                    }
                }
        write_blob(
            desktop
            / "IndexedDB"
            / "https_app.slack.com_0.indexeddb.blob"
            / "1"
            / "00"
            / str(blob_n),
            state,
            v8_version=16,
        )
        run_snapshot_once(Settings(data_dir=data, source_overrides=[desktop]))

    repo = SqliteRepository(data / "archive.db", read_only=True)
    for ts in ("1763400001.000200", "1763400999.000100"):
        row = repo._conn.execute(  # noqa: SLF001
            "SELECT deleted_state FROM messages WHERE channel_id=? AND ts=?",
            ("C0SYNTHPUB1", ts),
        ).fetchone()
        assert row is not None
        assert row["deleted_state"] != "suspected"
    repo.close()


def test_doctor_skips_edge_session_storage(tmp_path: Path) -> None:
    from slack_cache_mcp.desktop.doctor import run_doctor

    edge = build_edge_browser_profile(
        tmp_path / "Microsoft" / "Edge" / "User Data" / "Default"
    )
    settings = Settings(data_dir=tmp_path / "data", source_overrides=[edge])
    report = run_doctor(settings)
    assert report["edge_sources"] == 1
    assert report["session_storage"].get("skipped") == "edge_idb_only"
    assert report["local_storage_key_patterns"] == []
    assert report["http_cache"].get("skipped") == "edge_idb_only"


def test_edge_beta_override_classified_win_edge(tmp_path: Path) -> None:
    edge = tmp_path / "Microsoft" / "Edge Beta" / "User Data" / "Default"
    build_edge_browser_profile(edge)
    found = discover_profiles(overrides=[edge])
    assert len(found) == 1
    assert found[0].kind == "win-edge"


def test_edge_snapshot_rejects_symlink_escape(tmp_path: Path) -> None:
    src = build_edge_browser_profile(tmp_path / "Edge" / "User Data" / "Default")
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("xoxc-FAKE-TOKEN", encoding="utf-8")
    blob_dir = src / "IndexedDB" / "https_app.slack.com_0.indexeddb.blob" / "1" / "00"
    link = blob_dir / "escape"
    try:
        link.symlink_to(outside)
    except OSError:
        # Windows without symlink privilege — skip rather than false-fail.
        import pytest

        pytest.skip("symlinks unavailable")
    mirror = tmp_path / "mirror"
    stats = snapshot_profile(src, mirror, require_stable_blobs=False, allowlist=ALLOWLIST_EDGE_IDB)
    assert not (mirror / "IndexedDB" / "https_app.slack.com_0.indexeddb.blob" / "1" / "00" / "escape").exists()
    assert any("symlink" in e for e in stats.errors)


def test_edge_purge_removes_prior_desktop_allowlist_leftovers(tmp_path: Path) -> None:
    src = build_edge_browser_profile(tmp_path / "Edge" / "User Data" / "Default")
    mirror = tmp_path / "mirror"
    # Simulate a prior mistaken desktop-allowlist ingest.
    (mirror / "Local Storage" / "leveldb").mkdir(parents=True)
    (mirror / "Local Storage" / "leveldb" / "CURRENT").write_text("bait", encoding="utf-8")
    (mirror / "Session Storage").mkdir(parents=True)
    other = mirror / "IndexedDB" / "https_other.example_0.indexeddb.leveldb"
    other.mkdir(parents=True)
    (other / "CURRENT").write_text("other", encoding="utf-8")
    stats = snapshot_profile(src, mirror, require_stable_blobs=False, allowlist=ALLOWLIST_EDGE_IDB)
    assert stats.copied > 0
    assert not (mirror / "Local Storage").exists()
    assert not (mirror / "Session Storage").exists()
    assert not other.exists()


def test_list_edge_sighted_ts_excludes_historical(tmp_path: Path) -> None:
    from slack_cache_mcp.store.sqlite.repo import SqliteRepository

    db = tmp_path / "archive.db"
    repo = SqliteRepository(db)
    repo._conn.execute(  # noqa: SLF001
        """
        INSERT INTO message_sightings(
          channel_id, ts, content_sha256, device_id, source,
          first_seen, last_seen, sightings
        ) VALUES
          ('C1', '1.0', 'sha-live', 'd', 'edge.idb.blob',
           '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z', 1),
          ('C1', '2.0', 'sha-hist', 'd', 'edge.idb.leveldb.historical',
           '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z', 1)
        """
    )
    repo._conn.commit()  # noqa: SLF001
    assert repo.list_edge_sighted_ts("C1", {"1.0", "2.0"}) == {"1.0"}
    repo.close()


def test_daemon_reconciles_new_edge_watch_roots(tmp_path: Path) -> None:
    from slack_cache_mcp.daemon import _watch_root_key, _watch_roots

    edge = tmp_path / "Microsoft" / "Edge" / "User Data" / "Default"
    # Before Slack IDB exists: no watch roots.
    edge.mkdir(parents=True)
    assert _watch_roots(edge, edge_browser=True) == []
    build_edge_browser_profile(edge)
    roots = _watch_roots(edge, edge_browser=True)
    assert len(roots) == 2
    keys = {_watch_root_key(r) for r in roots}
    assert len(keys) == 2
    # Desktop extras must never be watched for Edge.
    assert all("Local Storage" not in str(r) for r in roots)
    assert all(r.name.endswith((".blob", ".leveldb")) or ".indexeddb." in r.name for r in roots)


def test_full_synthetic_desktop_still_works(tmp_path: Path) -> None:
    """Regression: standard desktop fixture ingest unchanged."""
    profile = build_synthetic_profile(tmp_path / "profile")
    settings = load_settings(data_dir=tmp_path / "data", source=profile)
    r = run_snapshot_once(settings)
    assert r["states_decoded"] == 3
    assert r["messages_ingested"] > 0
