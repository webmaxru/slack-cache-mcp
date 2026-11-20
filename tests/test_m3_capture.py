"""M3 maximum-capture: slices, canvas, historical, opt-ins, export."""

from __future__ import annotations

import json
from pathlib import Path

from slack_cache_mcp.config import load_settings
from slack_cache_mcp.desktop.chromium.idb_backend import detect_idb_backend, open_idb_backend
from slack_cache_mcp.desktop.doctor import run_doctor
from slack_cache_mcp.desktop.slack import canvas, redux
from slack_cache_mcp.export import write_slack_export
from slack_cache_mcp.pipeline import run_snapshot_once
from slack_cache_mcp.store.sqlite.repo import SqliteRepository
from tests.fixture_gen.build_profile import build_synthetic_profile


def test_redux_extra_slices_extract() -> None:
    from tests.fixture_gen.build_profile import build_states

    _pk, _rel, state = build_states()[0]
    ws, _ch, users, files, messages, reactions, saved = redux.extract_all(state)
    assert ws.workspace_key
    assert any(m.source == "redux.searchResults" for m in messages)
    assert any(m.source == "redux.channelLatests" and m.is_partial for m in messages)
    assert reactions
    assert saved
    assert files
    assert any(u.is_bot and u.user_id.startswith("B") for u in users)


def test_canvas_rebuild() -> None:
    docs = canvas.rebuild_canvases(
        [
            {
                "key": "syncer.Document/Fc1",
                "value": {"_type": "Document", "id": "Fc1", "title": "T", "channel_id": "C1"},
            },
            {
                "key": "syncer.Section/s1",
                "value": {
                    "_type": "Section",
                    "id": "s1",
                    "document_id": "Fc1",
                    "text": "Hello canvas",
                },
            },
            {
                "key": "syncer.LoadData.Response.Index/Section/Fc1",
                "value": {
                    "_type": "SectionIndex",
                    "document_id": "Fc1",
                    "section_ids": ["s1"],
                },
            },
        ],
        workspace_key="T1",
    )
    assert len(docs) == 1
    assert docs[0].title == "T"
    assert "Hello canvas" in docs[0].text


def test_idb_backend_sidecar_and_detect(tmp_path: Path) -> None:
    profile = build_synthetic_profile(tmp_path / "profile")
    assert detect_idb_backend(profile) == "leveldb"
    backend = open_idb_backend(profile)
    dbs = backend.list_databases()
    assert any(d.startswith("canvas-") for d in dbs)
    recs = list(backend.iter_records())
    assert any(r.source == "idb.sidecar" for r in recs)
    assert any(
        isinstance(r.value, dict) and r.value.get("text", "").startswith("historical")
        for r in recs
        if isinstance(r.value, dict)
    )


def test_pipeline_m3_sources_and_yield(tmp_path: Path) -> None:
    profile = build_synthetic_profile(tmp_path / "profile")
    data = tmp_path / "data"
    settings = load_settings(data_dir=data, source=profile)
    settings.capture_drafts = True
    settings.capture_downloads = True
    settings.capture_http_cache = True
    settings.capture_notifications = True
    result = run_snapshot_once(settings)
    assert result["states_decoded"] == 3
    assert result["token_leaks"] == []
    sy = result["sources"][0]["source_yield"]
    assert sy.get("redux.searchResults", {}).get("messages", 0) >= 1
    assert sy.get("canvas", {}).get("canvases", 0) >= 1
    assert sy.get("idb.leveldb.historical", {}).get("messages", 0) >= 1 or sy.get(
        "session.storage", {}
    ).get("messages", 0) >= 1
    assert sy.get("local.drafts", {}).get("drafts", 0) >= 1
    assert sy.get("root.downloads", {}).get("files", 0) >= 1
    assert sy.get("http.cache", {}).get("cache_entries", 0) >= 1
    assert sy.get("notif.win", {}).get("messages", 0) >= 1

    repo = SqliteRepository(settings.archive_db, read_only=True)
    assert repo.list_canvases()
    assert repo.list_saved()
    assert repo.list_files()
    assert repo.source_yield_report()
    # search hit from searchResults
    from slack_cache_mcp.search.service import SearchService

    hits = SearchService(repo).search_messages(query="elsewhere", mode="lexical", limit=5)
    assert hits["results"]
    doctor = run_doctor(settings)
    assert doctor["source_yield"]
    assert doctor["indexeddb_backend"] == "leveldb"
    repo.close()


def test_slack_export_layout(tmp_path: Path) -> None:
    profile = build_synthetic_profile(tmp_path / "profile")
    data = tmp_path / "data"
    settings = load_settings(data_dir=data, source=profile)
    run_snapshot_once(settings)
    repo = SqliteRepository(settings.archive_db, read_only=True)
    out = tmp_path / "export"
    result = write_slack_export(repo, out)
    assert result["ok"]
    assert (out / "channels.json").is_file()
    assert (out / "users.json").is_file()
    channels = json.loads((out / "channels.json").read_text(encoding="utf-8"))
    assert isinstance(channels, list)
    # at least one channel day file
    day_files = list(out.glob("*/*.json"))
    assert day_files
    # no xox in export
    blob = b"".join(p.read_bytes() for p in out.rglob("*.json"))
    assert b"xox" not in blob.lower()
    repo.close()
