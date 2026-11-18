"""One-shot snapshot -> decode -> ingest pipeline (M1)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import ulid

from slack_cache_mcp.config import Settings
from slack_cache_mcp.desktop.chromium.idb_values import decode_blob_file, idb_origin_dir_name
from slack_cache_mcp.desktop.locate import discover_profiles, redact_path
from slack_cache_mcp.desktop.sanitize import sanitize
from slack_cache_mcp.desktop.slack import localstate, redux, rootstate
from slack_cache_mcp.desktop.snapshot import snapshot_profile
from slack_cache_mcp.ingest.hlc import HLC
from slack_cache_mcp.store.sqlite.repo import SqliteRepository


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _device_id(data_dir: Path) -> str:
    path = data_dir / "device.json"
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data["device_id"])
    did = str(ulid.new())
    data_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"device_id": did, "label": "local"}, indent=2), encoding="utf-8")
    return did


def _source_id(device_id: str, path: Path) -> str:
    raw = f"{device_id}:{path.resolve().as_posix()}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def _state_sha(state: Any) -> str:
    clean = sanitize(state)
    blob = json.dumps(clean, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def run_snapshot_once(settings: Settings) -> dict[str, Any]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    device_id = _device_id(settings.data_dir)
    profiles = discover_profiles(overrides=settings.source_overrides or None)
    valid = [p for p in profiles if p.valid]
    if not valid:
        # also accept override path that exists even if validity check failed on empty
        if settings.source_overrides:
            from slack_cache_mcp.desktop.locate import SlackProfile

            valid = [
                SlackProfile(path=Path(p), kind="override", valid=True, primary=True)
                for p in settings.source_overrides
                if Path(p).exists()
            ]
    if not valid:
        return {"error": "no_valid_profiles", "profiles": []}

    primary = next((p for p in valid if p.primary), valid[0])
    source_id = _source_id(device_id, primary.path)
    mirror = settings.mirror_dir / source_id
    stats = snapshot_profile(primary.path, mirror)

    repo = SqliteRepository(settings.archive_db)
    repo.upsert_device(device_id, "local")
    repo.upsert_source(
        source_id,
        device_id,
        primary.kind,
        redact_path(primary.path),
        None,
    )
    snapshot_id = str(ulid.new())
    repo.begin_snapshot(snapshot_id, source_id, "manual")

    hlc = HLC(device_id=device_id)
    seen_at = _utc_now()
    states_decoded = 0
    decode_errors = 0
    messages_ingested = 0

    # Enrich workspaces from localConfig / root-state
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
            ws, channels, users, files, messages = redux.extract_all(clean)
            # Prefer names from localConfig / root-state
            if ws.workspace_key in teams:
                t = teams[ws.workspace_key]
                ws.name = ws.name or t.get("name")
                ws.domain = ws.domain or t.get("domain")
            if ws.workspace_key in root_ws:
                t = root_ws[ws.workspace_key]
                ws.name = ws.name or t.get("name")
                ws.domain = ws.domain or t.get("domain")

            if repo.has_state_generation(sha):
                repo.touch_state_generation(sha, seen_at)
                # Still refresh last_seen on directory? skip heavy re-extract for idempotency
                # but PLAN says update last_seen and stop — for messages we still want
                # sightings last_seen bumps. Re-ingest observations is idempotent.
            else:
                blob_ref = bf.relative_to(blob_root).as_posix()
                repo.insert_state_generation(
                    sha,
                    ws.workspace_key,
                    ws.self_user_id,
                    blob_ref,
                    snapshot_id,
                    seen_at,
                )

            tick = hlc.now()
            repo.upsert_workspace(ws, tick, seen_at)
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

    # Ensure root-state workspaces exist even without blobs
    for key, meta in root_ws.items():
        from slack_cache_mcp.domain.models import Workspace

        if not any(
            w["key"] == key for w in repo.list_workspaces()
        ):
            repo.upsert_workspace(
                Workspace(
                    workspace_key=key,
                    team_id=meta.get("teamId") or meta.get("id"),
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
                "errors": stats.errors[:20],
                "messages_ingested": messages_ingested,
            }
        ),
    )

    leaks = repo.scan_for_token_leak()
    return {
        "source": redact_path(primary.path),
        "source_id": source_id,
        "snapshot_id": snapshot_id,
        "files_copied": stats.copied,
        "bytes_copied": stats.bytes,
        "states_decoded": states_decoded,
        "decode_errors": decode_errors,
        "messages_ingested": messages_ingested,
        "token_leaks": leaks,
        "workspaces": repo.list_workspaces(),
    }
