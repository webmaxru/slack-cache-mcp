"""Coverage windows + suspected deletion across generations."""

from __future__ import annotations

from pathlib import Path

from slack_cache_mcp.config import load_settings
from slack_cache_mcp.ingest.coverage import (
    extract_channel_windows,
    suspected_missing,
    window_from_channel_messages,
)
from slack_cache_mcp.pipeline import run_snapshot_cycle
from slack_cache_mcp.store.sqlite.repo import SqliteRepository
from tests.fixture_gen.build_profile import (
    _msg,  # noqa: PLC2701
    write_blob,
)


def test_suspected_missing_in_window() -> None:
    win = window_from_channel_messages(
        "C1",
        ["1.0", "2.0", "4.0"],
        contiguous_to_latest=True,
    )
    assert win is not None
    missing = suspected_missing(previously_seen={"1.0", "2.0", "3.0", "9.0"}, window=win)
    # 3.0 is inside [1,4] but absent; 9.0 is outside → eviction, not deletion
    assert missing == {"3.0"}


def test_extract_windows_respects_has_more() -> None:
    state = {
        "messages": {"C1": {"1.0": {"ts": "1.0"}, "2.0": {"ts": "2.0"}}},
        "channelHistory": {"C1": {"has_more": True}},
    }
    wins = extract_channel_windows(state)
    assert len(wins) == 1
    assert wins[0].contiguous_to_latest is False


def _write_gen_profile(dest: Path, *, include_mid: bool) -> Path:
    """Minimal one-workspace profile; optionally drop middle message."""
    dest.mkdir(parents=True, exist_ok=True)
    idb_level = dest / "IndexedDB" / "https_app.slack.com_0.indexeddb.leveldb"
    idb_blob = dest / "IndexedDB" / "https_app.slack.com_0.indexeddb.blob"
    idb_level.mkdir(parents=True, exist_ok=True)
    (idb_level / "CURRENT").write_text("MANIFEST-000001\n", encoding="utf-8")
    (idb_level / "MANIFEST-000001").write_bytes(b"\x00")
    (idb_level / "LOCK").write_text("held", encoding="utf-8")

    c = "C0COVTEST01"
    uid = "U0COVSELF1"
    wk = "T0COVTESTA"
    msgs = {
        "1763401000.000100": _msg("1763401000.000100", uid, "keep-a", channel=c),
        "1763401002.000300": _msg("1763401002.000300", uid, "keep-c", channel=c),
    }
    if include_mid:
        msgs["1763401001.000200"] = _msg("1763401001.000200", uid, "vanish-b", channel=c)

    state = {
        "channels": {
            c: {
                "id": c,
                "name": "coverage",
                "is_channel": True,
                "is_private": False,
                "is_member": True,
                "context_team_id": wk,
            }
        },
        "members": {
            uid: {
                "id": uid,
                "name": "self",
                "real_name": "Self",
                "profile": {"display_name": "self"},
                "team_id": wk,
                "is_bot": False,
            }
        },
        "messages": {c: msgs},
        "threads": {},
        "teams": {wk: {"id": wk, "name": "Cov", "domain": "cov"}},
        "selfTeamIds": {"teamId": wk, "defaultWorkspaceId": wk},
        "bootData": {"team_id": wk, "user_id": uid, "team_url": "https://cov.example.test/"},
        "files": {},
    }
    write_blob(idb_blob / "1" / "00" / "1", state)
    ls = dest / "Local Storage"
    ls.mkdir(parents=True, exist_ok=True)
    (ls / "localConfig_v2.json").write_text("{}", encoding="utf-8")
    storage = dest / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    (storage / "root-state.json").write_text("{}", encoding="utf-8")
    return dest


def test_suspected_after_two_generations(tmp_path: Path) -> None:
    data = tmp_path / "data"
    # Gen 1: three messages
    p1 = _write_gen_profile(tmp_path / "p1", include_mid=True)
    s1 = load_settings(data_dir=data, source=p1)
    r1 = run_snapshot_cycle(s1, trigger="manual")
    assert r1["generations_new"] == 1

    repo = SqliteRepository(s1.archive_db, read_only=True)
    mid = "1763401001.000200"
    row = repo._conn.execute(  # noqa: SLF001
        "SELECT deleted_state FROM messages WHERE ts=?", (mid,)
    ).fetchone()
    assert row is not None
    assert row["deleted_state"] == "none"
    repo.close()

    # Gen 2: drop middle (still inside window) — streak 1, not yet suspected
    p2 = _write_gen_profile(tmp_path / "p2", include_mid=False)
    s2 = load_settings(data_dir=data, source=p2)
    r2 = run_snapshot_cycle(s2, trigger="watch")
    assert r2["generations_new"] == 1
    repo2 = SqliteRepository(s2.archive_db, read_only=True)
    row2 = repo2._conn.execute(  # noqa: SLF001
        "SELECT deleted_state FROM messages WHERE ts=?", (mid,)
    ).fetchone()
    assert row2["deleted_state"] == "none"
    repo2.close()

    # Gen 3: still absent → suspected (new state hash via edited keep-a text)
    import shutil

    from tests.fixture_gen.build_profile import write_blob as wb

    p3 = tmp_path / "p3"
    if p3.exists():
        shutil.rmtree(p3)
    p3 = _write_gen_profile(p3, include_mid=False)

    c = "C0COVTEST01"
    uid = "U0COVSELF1"
    wk = "T0COVTESTA"
    msgs = {
        "1763401000.000100": _msg("1763401000.000100", uid, "keep-a-v2", channel=c),
        "1763401002.000300": _msg("1763401002.000300", uid, "keep-c", channel=c),
    }
    state = {
        "channels": {
            c: {
                "id": c,
                "name": "coverage",
                "is_channel": True,
                "is_private": False,
                "is_member": True,
                "context_team_id": wk,
            }
        },
        "members": {
            uid: {
                "id": uid,
                "name": "self",
                "real_name": "Self",
                "profile": {"display_name": "self"},
                "team_id": wk,
                "is_bot": False,
            }
        },
        "messages": {c: msgs},
        "threads": {},
        "teams": {wk: {"id": wk, "name": "Cov", "domain": "cov"}},
        "selfTeamIds": {"teamId": wk, "defaultWorkspaceId": wk},
        "bootData": {"team_id": wk, "user_id": uid, "team_url": "https://cov.example.test/"},
        "files": {},
    }
    wb(p3 / "IndexedDB" / "https_app.slack.com_0.indexeddb.blob" / "1" / "00" / "1", state)

    s3 = load_settings(data_dir=data, source=p3)
    r3 = run_snapshot_cycle(s3, trigger="full")
    assert r3["generations_new"] == 1
    assert r3["suspected_marked"] >= 1
    repo3 = SqliteRepository(s3.archive_db, read_only=True)
    row3 = repo3._conn.execute(  # noqa: SLF001
        "SELECT deleted_state FROM messages WHERE ts=?", (mid,)
    ).fetchone()
    assert row3["deleted_state"] == "suspected"
    # Message still present in archive (never hard-deleted)
    assert (
        repo3._conn.execute(  # noqa: SLF001
            "SELECT COUNT(*) AS c FROM messages WHERE ts=?", (mid,)
        ).fetchone()["c"]
        == 1
    )
    cov = repo3._conn.execute(  # noqa: SLF001
        "SELECT COUNT(*) AS c FROM coverage_ranges"
    ).fetchone()["c"]
    assert cov >= 1
    repo3.close()
