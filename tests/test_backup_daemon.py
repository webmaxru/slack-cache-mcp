"""Backup API + short daemon cycle + task XML + encryption spike."""

from __future__ import annotations

import threading
from pathlib import Path

from slack_cache_mcp.backup import backup_archive
from slack_cache_mcp.config import load_settings
from slack_cache_mcp.crypto_spike import run_encryption_spike
from slack_cache_mcp.daemon import run_daemon
from slack_cache_mcp.pipeline import run_snapshot_once
from slack_cache_mcp.task_windows import generate_task_xml
from tests.fixture_gen.build_profile import build_synthetic_profile


def test_backup_archive(tmp_path: Path) -> None:
    profile = build_synthetic_profile(tmp_path / "profile")
    data = tmp_path / "data"
    settings = load_settings(data_dir=data, source=profile)
    run_snapshot_once(settings)
    result = backup_archive(settings.archive_db, settings.backups_dir)
    assert result["ok"] is True
    assert Path(result["path"]).is_file()
    assert result["bytes"] > 0


def test_daemon_max_cycles(tmp_path: Path) -> None:
    profile = build_synthetic_profile(tmp_path / "profile")
    data = tmp_path / "data"
    settings = load_settings(data_dir=data, source=profile)
    stop = threading.Event()
    # ensure_single=False: developer machines often already run slackcache daemon
    # (Windows named mutex Local\slackcache-daemon); this test covers cycle cadence.
    result = run_daemon(
        settings,
        ensure_single=False,
        max_cycles=1,
        poll_interval_s=0.05,
        full_pass_interval_s=60.0,
        stop_event=stop,
    )
    assert result["ok"] is True
    assert result["metrics"]["cycles"] >= 1
    assert result["metrics"]["full_triggers"] >= 1


def test_task_xml_contains_daemon() -> None:
    xml = generate_task_xml(command=["C:\\tools\\slackcache.exe"], data_dir=Path("D:\\data"))
    assert "slackcache-daemon" in xml or "slackcache" in xml
    assert "daemon" in xml
    assert "--ensure-single" in xml
    assert "IgnoreNew" in xml
    assert "PT5M" in xml


def test_encryption_spike_decision() -> None:
    report = run_encryption_spike()
    assert report["decision"] == "bitlocker_efs_vault_only"
    assert "stdlib_sqlite" in report
