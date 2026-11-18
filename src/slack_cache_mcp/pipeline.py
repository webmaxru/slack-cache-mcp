"""One-shot snapshot -> decode -> ingest pipeline (M1)."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import ulid

from slack_cache_mcp.config import Settings
from slack_cache_mcp.desktop.chromium.idb_values import decode_blob_file, idb_origin_dir_name
from slack_cache_mcp.desktop.locate import SlackProfile, discover_profiles, redact_path
from slack_cache_mcp.desktop.sanitize import sanitize
from slack_cache_mcp.desktop.slack import localstate, redux, rootstate
from slack_cache_mcp.desktop.snapshot import snapshot_profile
from slack_cache_mcp.domain.models import Channel, MessageObservation, Workspace
from slack_cache_mcp.ingest.hlc import HLC
from slack_cache_mcp.paths import assert_data_dir_outside_sources, secure_data_dir
from slack_cache_mcp.store.sqlite.repo import SqliteRepository


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _device_id(data_dir: Path) -> str:
    path = data_dir / "device.json"
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data["device_id"])
    did = str(ulid.new())
    path.write_text(json.dumps({"device_id": did, "label": "local"}, indent=2), encoding="utf-8")
    return did


def _source_id(device_id: str, path: Path) -> str:
    raw = f"{device_id}:{path.resolve().as_posix()}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def _state_sha(state: Any) -> str:
    clean = sanitize(state)
    blob = json.dumps(clean, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _scrub_credential_scratch(mirror: Path) -> None:
    """Remove Local Storage copies that may contain tokens; drop manifest entries."""
    targets = [
        mirror / "Local Storage" / "leveldb",
        mirror / "Local Storage" / "localConfig_v2.json",
    ]
    for t in targets:
        if t.is_file():
            t.unlink(missing_ok=True)
        elif t.is_dir():
            shutil.rmtree(t, ignore_errors=True)
    manifest_path = mirror / "manifest.json"
    if not manifest_path.is_file():
        return
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return
    drop_prefixes = ("Local Storage/leveldb/", "Local Storage/localConfig_v2.json")
    for key in list(files):
        if key == "Local Storage/localConfig_v2.json" or key.startswith("Local Storage/leveldb/"):
            files.pop(key, None)
    for pref in drop_prefixes:
        files.pop(pref, None)
    data["files"] = files
    manifest_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _filter_dms(
    channels: list[Channel],
    messages: list[MessageObservation],
    *,
    capture_dms: bool,
) -> tuple[list[Channel], list[MessageObservation]]:
    if capture_dms:
        return channels, messages
    dm_ids = {c.channel_id for c in channels if c.kind in {"im", "mpim"}}
    channels = [c for c in channels if c.channel_id not in dm_ids]
    messages = [m for m in messages if m.channel_id not in dm_ids]
    return channels, messages


def _ingest_profile(
    *,
    profile: SlackProfile,
    settings: Settings,
    device_id: str,
    repo: SqliteRepository,
    hlc: HLC,
    seen_at: str,
) -> dict[str, Any]:
    source_id = _source_id(device_id, profile.path)
    mirror = settings.mirror_dir / source_id
    assert_data_dir_outside_sources(settings.data_dir, [profile.path])
    assert_data_dir_outside_sources(mirror, [profile.path])

    stats = snapshot_profile(profile.path, mirror)
    repo.upsert_source(
        source_id,
        device_id,
        profile.kind,
        redact_path(profile.path),
        None,
    )
    snapshot_id = str(ulid.new())
    repo.begin_snapshot(snapshot_id, source_id, "manual")

    states_decoded = 0
    decode_errors = 0
    messages_ingested = 0
    skipped_known = 0

    try:
        teams = localstate.load_local_config_teams(mirror)
        root_ws = rootstate.load_root_workspaces(mirror)

        blob_root = mirror / "IndexedDB" / f"{idb_origin_dir_name()}.blob"
        if blob_root.is_dir():
            for bf in sorted(blob_root.rglob("*")):
                if not bf.is_file():
                    continue
                info, val = decode_blob_file(bf)
                if info.error or not isinstance(val, dict):
                    decode_errors += 1
                    continue
                states_decoded += 1
                clean = sanitize(val)
                assert isinstance(clean, dict)
                sha = _state_sha(clean)
                if repo.has_state_generation(sha):
                    repo.touch_state_generation(sha, seen_at)
                    skipped_known += 1
                    continue

                ws, channels, users, _files, messages = redux.extract_all(clean)
                if ws.workspace_key in teams:
                    t = teams[ws.workspace_key]
                    ws.name = ws.name or t.get("name")
                    ws.domain = ws.domain or t.get("domain")
                if ws.workspace_key in root_ws:
                    t = root_ws[ws.workspace_key]
                    ws.name = ws.name or t.get("name")
                    ws.domain = ws.domain or t.get("domain")

                channels, messages = _filter_dms(
                    channels, messages, capture_dms=settings.capture_dms
                )

                blob_ref = bf.relative_to(blob_root).as_posix()
                repo.insert_state_generation(
                    sha,
                    ws.workspace_key,
                    ws.self_user_id,
                    blob_ref,
                    snapshot_id,
                    seen_at,
                )
                repo.upsert_workspace(ws, hlc.now(), seen_at)
                for ch in channels:
                    repo.upsert_channel(ch, hlc.now(), seen_at)
                for u in users:
                    repo.upsert_user(u, hlc.now(), seen_at)
                for obs in messages:
                    repo.ingest_observation(
                        obs,
                        device_id=device_id,
                        snapshot_id=snapshot_id,
                        state_sha256=sha,
                        seen_at=seen_at,
                        hlc=hlc.now(),
                    )
                    messages_ingested += 1

        known_keys = {w["key"] for w in repo.list_workspaces()}
        for key, meta in root_ws.items():
            team_id = meta.get("teamId") or meta.get("id")
            if key in known_keys or team_id in known_keys:
                continue
            repo.upsert_workspace(
                Workspace(
                    workspace_key=key,
                    team_id=team_id,
                    enterprise_id=key if key.startswith("E") else None,
                    name=meta.get("name"),
                    domain=meta.get("domain"),
                    self_user_id=None,
                    self_is_guest=False,
                    raw=meta,
                ),
                hlc.now(),
                seen_at,
            )
            known_keys.add(key)
    finally:
        _scrub_credential_scratch(mirror)

    repo.finish_snapshot(
        snapshot_id,
        files_copied=stats.copied,
        bytes_copied=stats.bytes,
        states_decoded=states_decoded,
        decode_errors=decode_errors,
        stats_json=json.dumps(
            {
                "skipped_lock": stats.skipped_lock,
                "skipped_unchanged": stats.skipped_unchanged,
                "skipped_known_generations": skipped_known,
                "errors": stats.errors[:20],
                "messages_ingested": messages_ingested,
                "primary": profile.primary,
            }
        ),
    )
    return {
        "source": redact_path(profile.path),
        "source_id": source_id,
        "snapshot_id": snapshot_id,
        "files_copied": stats.copied,
        "bytes_copied": stats.bytes,
        "states_decoded": states_decoded,
        "decode_errors": decode_errors,
        "messages_ingested": messages_ingested,
        "skipped_known_generations": skipped_known,
        "primary": profile.primary,
    }


def run_snapshot_once(settings: Settings) -> dict[str, Any]:
    profiles = discover_profiles(overrides=settings.source_overrides or None)
    valid = [p for p in profiles if p.valid]
    if not valid and settings.source_overrides:
        valid = [
            SlackProfile(path=Path(p), kind="override", valid=True, primary=True)
            for p in settings.source_overrides
            if Path(p).exists()
        ]
    if not valid:
        return {"error": "no_valid_profiles", "profiles": []}

    # Reject overlap before creating any files under data_dir.
    assert_data_dir_outside_sources(settings.data_dir, [p.path for p in valid])
    secure_data_dir(settings.data_dir)
    device_id = _device_id(settings.data_dir)

    repo = SqliteRepository(settings.archive_db)
    repo.upsert_device(device_id, "local")
    hlc = HLC(device_id=device_id)
    seen_at = _utc_now()

    # PLAN §5.1: ingest every valid profile under its own source_id
    per_source: list[dict[str, Any]] = []
    for profile in valid:
        per_source.append(
            _ingest_profile(
                profile=profile,
                settings=settings,
                device_id=device_id,
                repo=repo,
                hlc=hlc,
                seen_at=seen_at,
            )
        )

    leaks = repo.scan_for_token_leak()
    primary = next((p for p in valid if p.primary), valid[0])
    totals = {
        "files_copied": sum(s["files_copied"] for s in per_source),
        "bytes_copied": sum(s["bytes_copied"] for s in per_source),
        "states_decoded": sum(s["states_decoded"] for s in per_source),
        "decode_errors": sum(s["decode_errors"] for s in per_source),
        "messages_ingested": sum(s["messages_ingested"] for s in per_source),
        "skipped_known_generations": sum(
            s["skipped_known_generations"] for s in per_source
        ),
    }
    return {
        "source": redact_path(primary.path),
        "sources": per_source,
        "source_id": per_source[0]["source_id"] if per_source else None,
        "snapshot_id": per_source[0]["snapshot_id"] if per_source else None,
        **totals,
        "token_leaks": leaks,
        "workspaces": repo.list_workspaces(),
    }
