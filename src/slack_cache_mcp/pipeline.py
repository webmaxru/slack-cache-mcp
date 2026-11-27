"""Snapshot -> decode -> vault -> ingest pipeline (M1 one-shot + M2 cycles)."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import ulid

from slack_cache_mcp.config import Settings
from slack_cache_mcp.desktop.chromium import httpcache
from slack_cache_mcp.desktop.chromium.idb_backend import detect_idb_backend, open_idb_backend
from slack_cache_mcp.desktop.chromium.idb_values import decode_blob_file, idb_origin_dir_name
from slack_cache_mcp.desktop.locate import SlackProfile, discover_profiles, redact_path
from slack_cache_mcp.desktop.sanitize import sanitize
from slack_cache_mcp.desktop.slack import (
    canvas,
    downloads,
    localstate,
    notifications_win,
    redux,
    rootstate,
    session_scan,
)
from slack_cache_mcp.desktop.snapshot import snapshot_profile
from slack_cache_mcp.domain.models import Channel, MessageObservation, Workspace
from slack_cache_mcp.ingest.coverage import extract_channel_windows, suspected_missing, ts_key
from slack_cache_mcp.ingest.hlc import HLC
from slack_cache_mcp.paths import assert_data_dir_outside_sources, secure_data_dir
from slack_cache_mcp.store.sqlite.repo import SqliteRepository
from slack_cache_mcp.vault.store import VaultStore


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _source_id(device_id: str, path: Path) -> str:
    raw = f"{device_id}:{path.resolve().as_posix()}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def _state_sha(state: Any) -> str:
    clean = sanitize(state)
    blob = json.dumps(clean, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class CredentialScratchError(RuntimeError):
    """Raised when Local Storage credential scratch cannot be removed from the mirror."""


def _rmtree_credential_scratch(path: Path) -> None:
    """Delete a path tree, clearing read-only bits; raise if anything remains."""
    import os
    import stat

    def _onexc(func: Any, p: str, exc: BaseException) -> None:
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception as retry_exc:
            raise CredentialScratchError(
                f"cannot remove credential scratch path {p}: {retry_exc}"
            ) from retry_exc

    if path.is_dir():
        shutil.rmtree(path, onexc=_onexc)
    elif path.is_file() or path.is_symlink():
        try:
            path.unlink()
        except PermissionError:
            os.chmod(path, stat.S_IWRITE)
            path.unlink()
    if path.exists():
        raise CredentialScratchError(f"credential scratch still present after delete: {path}")


def _scrub_credential_scratch(mirror: Path) -> None:
    """Physically delete Local Storage copies (may contain tokens) and drop manifest entries.

    PLAN §5.2: parse allowlisted fields, then securely remove the ephemeral mirror copy.
    Deletion failures are fatal — silent leftover token files are not acceptable.
    """
    local_storage = mirror / "Local Storage"
    if local_storage.exists():
        _rmtree_credential_scratch(local_storage)
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
    for key in list(files):
        if key == "Local Storage" or key.startswith("Local Storage/"):
            files.pop(key, None)
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


def _apply_coverage_and_suspected(
    repo: SqliteRepository,
    clean: dict[str, Any],
    *,
    state_sha256: str,
    seen_at: str,
) -> int:
    """Update coverage ranges / channel_status and mark suspected deletions. Returns count marked."""
    marked_total = 0
    latests = clean.get("channelLatests") if isinstance(clean.get("channelLatests"), dict) else {}
    unreads = clean.get("unreadCounts") if isinstance(clean.get("unreadCounts"), dict) else {}
    for win in extract_channel_windows(clean):
        repo.upsert_coverage_range(
            win.channel_id,
            win.start_ts,
            win.end_ts,
            contiguous_to_latest=win.contiguous_to_latest,
            seen_at=seen_at,
        )
        # Only load archive ts that fall inside this window (avoid O(N) full-channel scans).
        known_in_window = repo.list_known_message_ts(
            win.channel_id, start_ts=win.start_ts, end_ts=win.end_ts
        )
        ordered = sorted(win.message_ts, key=ts_key)
        latest_reported = None
        if isinstance(latests, dict):
            raw = latests.get(win.channel_id)
            if isinstance(raw, dict) and raw.get("ts"):
                latest_reported = str(raw["ts"])
            elif isinstance(raw, str):
                latest_reported = raw
        unread = None
        if isinstance(unreads, dict) and win.channel_id in unreads:
            u = unreads[win.channel_id]
            if isinstance(u, (int, float)):
                unread = int(u)
            elif isinstance(u, dict) and isinstance(u.get("count"), (int, float)):
                unread = int(u["count"])
        repo.update_channel_status(
            win.channel_id,
            newest_ts=ordered[-1] if ordered else None,
            oldest_ts=ordered[0] if ordered else None,
            captured_count=repo.count_messages(win.channel_id),
            latest_ts_reported=latest_reported,
            unread_count=unread,
            seen_at=seen_at,
        )
        if win.contiguous_to_latest:
            missing = suspected_missing(previously_seen=known_in_window, window=win)
            marked = repo.apply_suspected_deletions(
                win.channel_id,
                missing,
                state_sha256=state_sha256,
                seen_at=seen_at,
            )
            marked_total += len(marked)
    return marked_total


def _ingest_profile(
    *,
    profile: SlackProfile,
    settings: Settings,
    device_id: str,
    repo: SqliteRepository,
    vault: VaultStore,
    hlc: HLC,
    seen_at: str,
    trigger: str,
) -> dict[str, Any]:
    source_id = _source_id(device_id, profile.path)
    mirror = settings.mirror_dir / source_id
    assert_data_dir_outside_sources(settings.data_dir, [profile.path])
    assert_data_dir_outside_sources(mirror, [profile.path])

    stats = snapshot_profile(
        profile.path,
        mirror,
        include_http_cache=settings.capture_http_cache,
        include_notifications=settings.capture_notifications,
        http_cache_max_bytes=settings.http_cache_max_bytes,
    )
    repo.upsert_source(
        source_id,
        device_id,
        profile.kind,
        redact_path(profile.path),
        None,
    )
    snapshot_id = str(ulid.new())
    repo.begin_snapshot(snapshot_id, source_id, trigger)

    states_decoded = 0
    decode_errors = 0
    messages_ingested = 0
    skipped_known = 0
    generations_new = 0
    vault_writes = 0
    suspected_marked = 0
    yield_counts: dict[str, dict[str, int]] = {}
    idb_backend_kind = "unknown"

    def _bump(source: str, **kw: int) -> None:
        bucket = yield_counts.setdefault(
            source,
            {
                "messages": 0,
                "files": 0,
                "canvases": 0,
                "reactions": 0,
                "saved_items": 0,
                "drafts": 0,
                "cache_entries": 0,
                "errors": 0,
            },
        )
        for k, v in kw.items():
            bucket[k] = bucket.get(k, 0) + v

    try:
        teams = localstate.load_local_config_teams(mirror)
        root_ws = rootstate.load_root_workspaces(mirror)
        idb_backend_kind = detect_idb_backend(mirror)

        blob_root = mirror / "IndexedDB" / f"{idb_origin_dir_name()}.blob"
        if blob_root.is_dir():
            for bf in sorted(blob_root.rglob("*")):
                if not bf.is_file():
                    continue
                info, val = decode_blob_file(bf)
                if info.error or not isinstance(val, dict):
                    decode_errors += 1
                    _bump("redux.blob", errors=1)
                    continue
                states_decoded += 1
                clean = sanitize(val)
                assert isinstance(clean, dict)
                sha = _state_sha(clean)
                if repo.has_state_generation(sha):
                    repo.touch_state_generation(sha, seen_at)
                    skipped_known += 1
                    continue

                generations_new += 1
                vres = vault.write_state(clean, state_sha256=sha)
                vault_path = vres.relative_path if vres else None
                if vres and vres.created:
                    vault_writes += 1

                ws, channels, users, files, messages, reactions, saved = redux.extract_all(
                    clean
                )
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
                    vault_path=vault_path,
                )
                repo.upsert_workspace(ws, hlc.now(), seen_at, device_id=device_id)
                for ch in channels:
                    repo.upsert_channel(ch, hlc.now(), seen_at, device_id=device_id)
                for u in users:
                    repo.upsert_user(u, hlc.now(), seen_at, device_id=device_id)
                for f in files:
                    repo.upsert_file(f, hlc.now(), seen_at)
                    _bump("redux.files", files=1)
                for rx in reactions:
                    repo.upsert_reaction(rx, hlc.now())
                    _bump("redux.reactions", reactions=1)
                for item in saved:
                    repo.upsert_saved_item(item, seen_at)
                    _bump("redux.saved", saved_items=1)
                for obs in messages:
                    if repo.ingest_observation(
                        obs,
                        device_id=device_id,
                        snapshot_id=snapshot_id,
                        state_sha256=sha,
                        seen_at=seen_at,
                        hlc=hlc.now(),
                    ):
                        messages_ingested += 1
                        _bump(obs.source, messages=1)

                suspected_marked += _apply_coverage_and_suspected(
                    repo, clean, state_sha256=sha, seen_at=seen_at
                )

        # Historical / inline IDB records (LevelDB + side-cars)
        try:
            backend = open_idb_backend(mirror)
            hist_err = 0
            canvas_rows: list[Any] = []
            for rec in backend.iter_records(include_deleted=True):
                if rec.error:
                    hist_err += 1
                if rec.database.startswith("canvas") or (
                    isinstance(rec.key, str) and rec.key.startswith("syncer.")
                ):
                    canvas_rows.append(rec)
                if isinstance(rec.value, dict) and (
                    "messages" in rec.value or "bootData" in rec.value
                ):
                    # Full Redux state found inline/historical — extract if new
                    clean = sanitize(rec.value)
                    assert isinstance(clean, dict)
                    sha = _state_sha(clean)
                    if not repo.has_state_generation(sha):
                        ws, channels, users, files, messages, reactions, saved = (
                            redux.extract_all(clean)
                        )
                        channels, messages = _filter_dms(
                            channels, messages, capture_dms=settings.capture_dms
                        )
                        for obs in messages:
                            obs.source = (
                                "idb.leveldb.historical"
                                if rec.state == "deleted"
                                else (rec.source or "idb.leveldb")
                            )
                            obs.source_rank = redux.SOURCE_RANK.get(
                                obs.source, obs.source_rank
                            )
                            if repo.ingest_observation(
                                obs,
                                device_id=device_id,
                                snapshot_id=snapshot_id,
                                state_sha256=sha,
                                seen_at=seen_at,
                                hlc=hlc.now(),
                            ):
                                messages_ingested += 1
                                _bump(obs.source, messages=1)
                        repo.insert_state_generation(
                            sha,
                            ws.workspace_key,
                            ws.self_user_id,
                            None,
                            snapshot_id,
                            seen_at,
                            vault_path=None,
                        )
                        repo.upsert_workspace(ws, hlc.now(), seen_at, device_id=device_id)
                # Message-shaped single values from historical records
                if isinstance(rec.value, dict) and redux.is_message(rec.value):
                    cid = rec.value.get("channel")
                    if cid:
                        hist_obs = redux.observation_from_message(
                            rec.value,
                            channel_id=str(cid),
                            workspace_key="unknown",
                            source="idb.leveldb.historical",
                        )
                        if hist_obs is not None and repo.ingest_observation(
                            hist_obs,
                            device_id=device_id,
                            snapshot_id=snapshot_id,
                            state_sha256="idb-historical",
                            seen_at=seen_at,
                            hlc=hlc.now(),
                        ):
                            messages_ingested += 1
                            _bump("idb.leveldb.historical", messages=1)

            # Group canvas records by database
            by_db: dict[str, list[Any]] = {}
            for rec in canvas_rows:
                by_db.setdefault(rec.database, []).append(rec)
            for db_name, rows in by_db.items():
                for doc in canvas.extract_from_idb_records(rows, database=db_name):
                    repo.upsert_canvas(doc, seen_at)
                    _bump("canvas", canvases=1)

            # Note: message yields are already counted per-message under the concrete source
            # bucket (e.g., "idb.leveldb.historical"). Keep the umbrella bucket for non-message
            # aggregates like errors to avoid double-counting in yield reports.
            _bump("idb.historical", errors=hist_err)
        except Exception:  # noqa: BLE001 — never fail the cycle on optional harvest
            _bump("idb.historical", errors=1)

        # Session Storage scan
        for obs in session_scan.scan_session_messages(mirror):
            if repo.ingest_observation(
                obs,
                device_id=device_id,
                snapshot_id=snapshot_id,
                state_sha256="session",
                seen_at=seen_at,
                hlc=hlc.now(),
            ):
                messages_ingested += 1
                _bump("session.storage", messages=1)

        # Opt-in drafts
        if settings.capture_drafts:
            for draft in localstate.load_drafts(mirror):
                repo.upsert_draft(draft, seen_at)
                _bump("local.drafts", drafts=1)

        # Opt-in downloads
        if settings.capture_downloads:
            dl_rows = downloads.load_downloads(mirror)
            for f in downloads.index_local_downloads(dl_rows, extract_text=True):
                repo.upsert_file(f, hlc.now(), seen_at)
                _bump("root.downloads", files=1)

        # Opt-in HTTP cache
        if settings.capture_http_cache:
            entries = httpcache.extract_cache_entries(
                mirror, max_bytes_total=settings.http_cache_max_bytes
            )
            for ent in entries:
                _bump("http.cache", cache_entries=1)
                if settings.capture_ocr and ent.ocr_text is None:
                    pass  # OCR stub; no-op without platform OCR deps

        # Opt-in Windows notifications (side-car under mirror only in automated runs)
        if settings.capture_notifications:
            for obs in notifications_win.extract_notifications(mirror):
                if repo.ingest_observation(
                    obs,
                    device_id=device_id,
                    snapshot_id=snapshot_id,
                    state_sha256="notif",
                    seen_at=seen_at,
                    hlc=hlc.now(),
                ):
                    messages_ingested += 1
                    _bump("notif.win", messages=1)

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
                device_id=device_id,
            )
            known_keys.add(key)
    finally:
        _scrub_credential_scratch(mirror)

    for src_name, counts in yield_counts.items():
        repo.record_source_yield(
            snapshot_id,
            src_name,
            messages=int(counts.get("messages", 0)),
            files=int(counts.get("files", 0)),
            canvases=int(counts.get("canvases", 0)),
            reactions=int(counts.get("reactions", 0)),
            saved_items=int(counts.get("saved_items", 0)),
            drafts=int(counts.get("drafts", 0)),
            cache_entries=int(counts.get("cache_entries", 0)),
            errors=int(counts.get("errors", 0)),
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
                "skipped_known_generations": skipped_known,
                "generations_new": generations_new,
                "vault_writes": vault_writes,
                "suspected_marked": suspected_marked,
                "errors": stats.errors[:20],
                "messages_ingested": messages_ingested,
                "primary": profile.primary,
                "trigger": trigger,
                "idb_backend": idb_backend_kind,
                "source_yield": yield_counts,
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
        "generations_new": generations_new,
        "vault_writes": vault_writes,
        "suspected_marked": suspected_marked,
        "source_yield": yield_counts,
        "primary": profile.primary,
    }


def run_snapshot_cycle(settings: Settings, *, trigger: str = "manual") -> dict[str, Any]:
    """One copy+decode+ingest cycle (used by snapshot --once and the daemon)."""
    profiles = discover_profiles(overrides=settings.source_overrides or None)
    valid = [p for p in profiles if p.valid]
    if not valid and settings.source_overrides:
        valid = [
            SlackProfile(path=Path(p), kind="override", valid=True, primary=True)
            for p in settings.source_overrides
            if Path(p).exists()
        ]
    if not valid:
        return {"error": "no_valid_profiles", "profiles": [], "trigger": trigger}

    assert_data_dir_outside_sources(settings.data_dir, [p.path for p in valid])
    secure_data_dir(settings.data_dir)
    from slack_cache_mcp.sync.bundle import ensure_local_device

    repo = SqliteRepository(settings.archive_db)
    identity = ensure_local_device(repo, settings.data_dir)
    device_id = identity.device_id
    vault = VaultStore(
        settings.vault_dir,
        data_dir=settings.data_dir,
        keep=settings.vault_keep,
        keep_days=settings.vault_keep_days,
    )
    hlc = HLC(device_id=device_id)
    seen_at = _utc_now()

    per_source: list[dict[str, Any]] = []
    for profile in valid:
        per_source.append(
            _ingest_profile(
                profile=profile,
                settings=settings,
                device_id=device_id,
                repo=repo,
                vault=vault,
                hlc=hlc,
                seen_at=seen_at,
                trigger=trigger,
            )
        )

    leaks = repo.scan_for_token_leak()
    vault_leaks = vault.scan_for_token_leak() if settings.vault_keep else []
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
        "generations_new": sum(s["generations_new"] for s in per_source),
        "vault_writes": sum(s["vault_writes"] for s in per_source),
        "suspected_marked": sum(s["suspected_marked"] for s in per_source),
    }
    return {
        "trigger": trigger,
        "source": redact_path(primary.path),
        "sources": per_source,
        "source_id": per_source[0]["source_id"] if per_source else None,
        "snapshot_id": per_source[0]["snapshot_id"] if per_source else None,
        **totals,
        "token_leaks": leaks,
        "vault_token_leaks": vault_leaks,
        "workspaces": repo.list_workspaces(),
    }


def run_snapshot_once(settings: Settings) -> dict[str, Any]:
    return run_snapshot_cycle(settings, trigger="manual")
