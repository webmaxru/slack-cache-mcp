"""Pipeline path/permission/DM/capture policy tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from slack_cache_mcp.config import Settings, load_settings
from slack_cache_mcp.paths import PathOverlapError, assert_data_dir_outside_sources
from slack_cache_mcp.pipeline import run_snapshot_once
from slack_cache_mcp.store.sqlite.repo import SqliteRepository
from tests.fixture_gen.build_profile import build_synthetic_profile


def test_reject_overlapping_data_dir(tmp_path: Path) -> None:
    profile = build_synthetic_profile(tmp_path / "profile")
    with pytest.raises(PathOverlapError):
        assert_data_dir_outside_sources(profile, [profile])
    settings = load_settings(data_dir=profile, source=profile)
    with pytest.raises(PathOverlapError):
        run_snapshot_once(settings)


def test_capture_dms_false_skips_im(tmp_path: Path) -> None:
    profile = build_synthetic_profile(tmp_path / "profile")
    settings = Settings(data_dir=tmp_path / "data", source_overrides=[profile], capture_dms=False)
    run_snapshot_once(settings)
    repo = SqliteRepository(settings.archive_db, read_only=True)
    ims = repo._conn.execute(  # noqa: SLF001
        "SELECT COUNT(*) AS c FROM channels WHERE kind IN ('im','mpim')"
    ).fetchone()["c"]
    assert ims == 0
    repo.close()


def test_local_storage_scrubbed_after_snapshot(tmp_path: Path) -> None:
    import json

    profile = build_synthetic_profile(tmp_path / "profile")
    # Plant an extra token-bearing file under Local Storage to prove whole-tree scrub.
    planted = profile / "Local Storage" / "extra" / "secret.txt"
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text("xoxc-FAKEPLANTED000000000000000000", encoding="utf-8")
    settings = load_settings(data_dir=tmp_path / "data", source=profile)
    result = run_snapshot_once(settings)
    mirror = settings.mirror_dir / result["source_id"]
    # Entire Local Storage tree must be physically removed after the cycle.
    assert not (mirror / "Local Storage").exists()
    manifest = json.loads((mirror / "manifest.json").read_text(encoding="utf-8"))
    files = manifest.get("files") if isinstance(manifest, dict) else None
    assert isinstance(files, dict)
    assert not any(
        key == "Local Storage" or str(key).startswith("Local Storage/") for key in files
    )


def test_known_generation_short_circuit(tmp_path: Path) -> None:
    profile = build_synthetic_profile(tmp_path / "profile")
    settings = load_settings(data_dir=tmp_path / "data", source=profile)
    r1 = run_snapshot_once(settings)
    assert r1["messages_ingested"] > 0
    r2 = run_snapshot_once(settings)
    assert r2["skipped_known_generations"] >= r1["states_decoded"]
    assert r2["messages_ingested"] == 0
