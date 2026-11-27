"""Apply sanitized change_log rows from peer bundles (idempotent G-Set union)."""

from __future__ import annotations

import contextlib
import json
from typing import Any

from slack_cache_mcp.desktop.sanitize import sanitize
from slack_cache_mcp.domain.models import Channel, MessageObservation, User, Workspace
from slack_cache_mcp.ingest.hlc import HLC
from slack_cache_mcp.store.sqlite.repo import SqliteRepository
from slack_cache_mcp.sync.outbox import payload_sha256


def _as_dict(payload: Any) -> dict[str, Any]:
    obj = json.loads(payload) if isinstance(payload, str) else payload
    if not isinstance(obj, dict):
        raise TypeError("payload must be a dict")
    clean = sanitize(obj)
    if not isinstance(clean, dict):
        raise TypeError("sanitized payload must be a dict")
    return clean


def apply_change_row(
    repo: SqliteRepository,
    row: dict[str, Any],
    *,
    hlc_clock: HLC | None = None,
    record_outbox: bool = True,
) -> tuple[bool, set[tuple[str, str]]]:
    """Apply one change_log row atomically.

    Returns ``(applied, reproject_keys)``. Idempotent on
    ``(device_id, entity, entity_key, payload_sha256)``.
    """
    device_id = str(row["device_id"])
    entity = str(row["entity"])
    entity_key = str(row["entity_key"])
    op = str(row.get("op") or "upsert")
    hlc = str(row["hlc"])
    payload = _as_dict(row.get("payload_json") or row.get("payload") or {})
    payload_sha = str(row.get("payload_sha256") or payload_sha256(payload))
    reproject: set[tuple[str, str]] = set()

    if hlc_clock is not None:
        hlc_clock.observe(hlc)

    if repo.has_change_log(
        device_id=device_id,
        entity=entity,
        entity_key=entity_key,
        payload_sha256=payload_sha,
    ):
        return False, reproject

    # Marker + fact updates in one transaction; rollback on any failure so a
    # failed FK/apply cannot permanently skip the row.
    try:
        repo._conn.execute("BEGIN")  # noqa: SLF001
        if record_outbox:
            inserted = repo.insert_change_log_row(
                hlc=hlc,
                device_id=device_id,
                entity=entity,
                entity_key=entity_key,
                op=op,
                payload=payload,
                payload_sha256=payload_sha,
                commit=False,
            )
            if not inserted:
                repo._conn.rollback()  # noqa: SLF001
                return False, reproject

        if entity == "message_version":
            _apply_message_version(repo, payload, device_id=device_id, hlc=hlc)
            ch = str(payload.get("channel_id") or "")
            ts = str(payload.get("ts") or "")
            if ch and ts:
                reproject.add((ch, ts))
        elif entity == "workspace":
            _apply_workspace(repo, payload, hlc=hlc)
        elif entity == "channel":
            _apply_channel(repo, payload, hlc=hlc)
            ch = str(payload.get("channel_id") or "")
            if ch:
                for ts in repo.list_known_message_ts(ch):
                    reproject.add((ch, ts))
        elif entity == "user":
            _apply_user(repo, payload, hlc=hlc)
        elif entity == "sighting":
            _apply_sighting(repo, payload, authenticated_device_id=device_id)
            ch = str(payload.get("channel_id") or "")
            ts = str(payload.get("ts") or "")
            if ch and ts:
                reproject.add((ch, ts))
        # Project + search-docs inside the same transaction so retries cannot
        # skip committed markers while leaving search/docs missing.
        for ch, ts in sorted(reproject):
            meta = repo.reproject_message(ch, ts, commit=False) or {}
            repo.refresh_message_search_docs(
                ch,
                ts,
                previous_thread_ts=meta.get("previous_thread_ts"),
                is_new_msg=bool(meta.get("is_new_msg")),
                commit=False,
            )
        repo._conn.commit()  # noqa: SLF001
    except Exception:
        with contextlib.suppress(Exception):
            repo._conn.rollback()  # noqa: SLF001
        raise
    return True, reproject


_ENTITY_ORDER = {
    "workspace": 0,
    "channel": 1,
    "user": 2,
    "message_version": 3,
    "sighting": 4,
}


def apply_change_rows(
    repo: SqliteRepository,
    rows: list[dict[str, Any]],
    *,
    hlc_clock: HLC | None = None,
) -> dict[str, int]:
    applied = 0
    skipped = 0
    touched: set[tuple[str, str]] = set()
    # Workspaces before channels (FK); messages before sightings.
    ordered = sorted(
        enumerate(rows),
        key=lambda item: (_ENTITY_ORDER.get(str(item[1].get("entity") or ""), 99), item[0]),
    )
    for _idx, row in ordered:
        ok, keys = apply_change_row(repo, row, hlc_clock=hlc_clock, record_outbox=True)
        if ok:
            applied += 1
            touched.update(keys)
        else:
            skipped += 1
    return {"applied": applied, "skipped": skipped, "reprojected": len(touched)}


def _apply_message_version(
    repo: SqliteRepository,
    payload: dict[str, Any],
    *,
    device_id: str,
    hlc: str,
) -> None:
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    clean_raw = sanitize(raw) if isinstance(raw, dict) else {}
    if not isinstance(clean_raw, dict):
        clean_raw = {}
    obs = MessageObservation(
        channel_id=str(payload["channel_id"]),
        ts=str(payload["ts"]),
        workspace_key=str(payload.get("workspace_key") or "unknown"),
        user_id=payload.get("user_id"),
        thread_ts=payload.get("thread_ts"),
        subtype=payload.get("subtype"),
        text=str(payload.get("text") or ""),
        search_text=str(payload.get("search_text") or payload.get("text") or ""),
        content_sha256=str(payload["content_sha256"]),
        edited_ts=payload.get("edited_ts"),
        is_deletion=bool(payload.get("is_deletion")),
        is_partial=bool(payload.get("is_partial")),
        source=str(payload.get("source") or "sync"),
        source_rank=int(payload.get("source_rank") or 0),
        raw=clean_raw,
        reply_count=payload.get("reply_count"),
        latest_reply=payload.get("latest_reply"),
        reply_users=list(payload.get("reply_users") or []),
    )
    first_seen = str(payload.get("first_seen") or "")
    last_seen = str(payload.get("last_seen") or first_seen or "")
    # Prefer real bounds when present; avoid inventing 1970 when both missing —
    # sightings later repair version timestamps via merge_version_sighting_times.
    seen_at = last_seen or first_seen or "1970-01-01T00:00:00Z"
    repo.ingest_observation(
        obs,
        device_id=device_id,
        snapshot_id=str(payload.get("snapshot_id") or "sync"),
        state_sha256=str(payload.get("state_sha256") or "sync"),
        seen_at=seen_at,
        hlc=hlc,
        write_change_log=False,
        write_sighting=False,
        commit=False,
        reproject=False,
    )
    if first_seen or last_seen:
        repo.merge_version_sighting_times(
            channel_id=obs.channel_id,
            ts=obs.ts,
            content_sha256=obs.content_sha256,
            first_seen=first_seen or seen_at,
            last_seen=last_seen or seen_at,
            commit=False,
        )
    # Sightings may have arrived earlier; pull their bounds onto this version.
    repo.reconcile_version_times_from_sightings(
        channel_id=obs.channel_id,
        ts=obs.ts,
        content_sha256=obs.content_sha256,
        commit=False,
    )


def _directory_clock_mode(
    repo: SqliteRepository, payload: dict[str, Any], incoming_field_hlcs: dict[str, str]
) -> tuple[dict[str, str], bool, bool]:
    """Resolve field-clock handling from ``clock_epoch`` vs local repair epoch.

    Returns ``(clocks, force_fields, fill_nulls_only)``.
    """
    try:
        incoming_epoch = int(payload.get("clock_epoch") or 0)
    except (TypeError, ValueError):
        incoming_epoch = 0
    local_epoch = repo.directory_clock_epoch()
    if incoming_epoch < local_epoch:
        # Obsolete / pre-repair facts: never trust their clocks; only fill nulls.
        return {}, False, True
    if incoming_epoch > local_epoch:
        repo.set_directory_clock_epoch(incoming_epoch, commit=False)
        repo.invalidate_directory_field_clocks(commit=False)
        return incoming_field_hlcs, True, False
    return incoming_field_hlcs, False, False


def _apply_workspace(repo: SqliteRepository, payload: dict[str, Any], *, hlc: str) -> None:
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    clean_raw = sanitize(raw) if isinstance(raw, dict) else {}
    if not isinstance(clean_raw, dict):
        clean_raw = {}
    ws = Workspace(
        workspace_key=str(payload["workspace_key"]),
        team_id=payload.get("team_id"),
        enterprise_id=payload.get("enterprise_id"),
        name=payload.get("name"),
        domain=payload.get("domain"),
        self_user_id=payload.get("self_user_id"),
        self_is_guest=bool(payload.get("self_is_guest")),
        raw=clean_raw,
    )
    seen_at = str(payload.get("last_seen") or payload.get("first_seen") or "1970-01-01T00:00:00Z")
    field_hlcs = payload.get("field_hlcs")
    incoming_field_hlcs = (
        {str(k): str(v) for k, v in field_hlcs.items() if isinstance(k, str) and isinstance(v, str)}
        if isinstance(field_hlcs, dict)
        else {}
    )
    clocks, force_fields, fill_nulls_only = _directory_clock_mode(
        repo, payload, incoming_field_hlcs
    )
    repo.upsert_workspace(
        ws,
        hlc,
        seen_at,
        write_change_log=False,
        hlc_lww=True,
        commit=False,
        incoming_field_hlcs=clocks,
        force_fields=force_fields,
        fill_nulls_only=fill_nulls_only,
    )


def _apply_channel(repo: SqliteRepository, payload: dict[str, Any], *, hlc: str) -> None:
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    clean_raw = sanitize(raw) if isinstance(raw, dict) else {}
    if not isinstance(clean_raw, dict):
        clean_raw = {}
    ch = Channel(
        channel_id=str(payload["channel_id"]),
        workspace_key=str(payload["workspace_key"]),
        context_team_id=payload.get("context_team_id"),
        name=payload.get("name"),
        kind=str(payload.get("kind") or "channel"),
        is_ext_shared=bool(payload.get("is_ext_shared")),
        is_archived=bool(payload.get("is_archived")),
        is_member=bool(payload.get("is_member")),
        topic=payload.get("topic"),
        purpose=payload.get("purpose"),
        im_user_id=payload.get("im_user_id"),
        raw=clean_raw,
    )
    seen_at = str(payload.get("last_seen") or payload.get("first_seen") or "1970-01-01T00:00:00Z")
    field_hlcs = payload.get("field_hlcs")
    incoming_field_hlcs = (
        {str(k): str(v) for k, v in field_hlcs.items() if isinstance(k, str) and isinstance(v, str)}
        if isinstance(field_hlcs, dict)
        else {}
    )
    clocks, force_fields, fill_nulls_only = _directory_clock_mode(
        repo, payload, incoming_field_hlcs
    )
    repo.upsert_channel(
        ch,
        hlc,
        seen_at,
        write_change_log=False,
        hlc_lww=True,
        commit=False,
        incoming_field_hlcs=clocks,
        force_fields=force_fields,
        fill_nulls_only=fill_nulls_only,
    )
    # Union observations independent of the LWW projection.
    for wk in payload.get("workspace_keys") or []:
        if isinstance(wk, str) and wk:
            repo._conn.execute(  # noqa: SLF001
                """
                INSERT OR IGNORE INTO channel_workspaces(channel_id, workspace_key)
                VALUES (?, ?)
                """,
                (ch.channel_id, wk),
            )
    alias_items: list[Any] = []
    if isinstance(payload.get("alias_records"), list):
        alias_items.extend(payload["alias_records"])
    if isinstance(payload.get("aliases"), list):
        alias_items.extend(payload["aliases"])
    for alias in alias_items:
        alias_name: str | None = None
        alias_seen = seen_at
        if isinstance(alias, str) and alias:
            alias_name = alias
        elif isinstance(alias, dict) and isinstance(alias.get("name"), str):
            alias_name = alias["name"]
            if isinstance(alias.get("first_seen"), str) and alias["first_seen"]:
                alias_seen = alias["first_seen"]
        if not alias_name:
            continue
        repo._conn.execute(  # noqa: SLF001
            """
            INSERT INTO channel_aliases(channel_id, name, first_seen)
            VALUES (?, ?, ?)
            ON CONFLICT(channel_id, name) DO UPDATE SET
              first_seen=CASE
                WHEN channel_aliases.first_seen > excluded.first_seen
                THEN excluded.first_seen ELSE channel_aliases.first_seen END
            """,
            (ch.channel_id, alias_name, alias_seen),
        )


def _apply_user(repo: SqliteRepository, payload: dict[str, Any], *, hlc: str) -> None:
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    clean_raw = sanitize(raw) if isinstance(raw, dict) else {}
    if not isinstance(clean_raw, dict):
        clean_raw = {}
    user = User(
        user_id=str(payload["user_id"]),
        team_id=payload.get("team_id"),
        name=payload.get("name"),
        real_name=payload.get("real_name"),
        display_name=payload.get("display_name"),
        title=payload.get("title"),
        email=payload.get("email"),
        is_bot=bool(payload.get("is_bot")),
        is_deleted=bool(payload.get("is_deleted")),
        is_restricted=bool(payload.get("is_restricted")),
        is_ultra_restricted=bool(payload.get("is_ultra_restricted")),
        is_external=bool(payload.get("is_external")),
        tz=payload.get("tz"),
        raw=clean_raw,
        workspace_key=payload.get("workspace_key"),
    )
    seen_at = str(payload.get("last_seen") or payload.get("first_seen") or "1970-01-01T00:00:00Z")
    workspace_keys = payload.get("workspace_keys")
    keys = (
        [str(k) for k in workspace_keys]
        if isinstance(workspace_keys, list)
        else None
    )
    field_hlcs = payload.get("field_hlcs")
    incoming_field_hlcs = (
        {str(k): str(v) for k, v in field_hlcs.items() if isinstance(k, str) and isinstance(v, str)}
        if isinstance(field_hlcs, dict)
        else {}
    )
    clocks, force_fields, fill_nulls_only = _directory_clock_mode(
        repo, payload, incoming_field_hlcs
    )
    repo.upsert_user(
        user,
        hlc,
        seen_at,
        write_change_log=False,
        hlc_lww=True,
        commit=False,
        workspace_keys=keys,
        incoming_field_hlcs=clocks,
        force_fields=force_fields,
        fill_nulls_only=fill_nulls_only,
    )


def _apply_sighting(
    repo: SqliteRepository,
    payload: dict[str, Any],
    *,
    authenticated_device_id: str,
) -> None:
    payload_device = str(payload.get("device_id") or "")
    if payload_device != authenticated_device_id:
        raise ValueError(
            "sighting payload device_id does not match authenticated sender"
        )
    first_seen = str(payload.get("first_seen") or payload.get("last_seen") or "")
    last_seen = str(payload.get("last_seen") or payload.get("first_seen") or "")
    if not last_seen:
        raise ValueError("sighting payload missing last_seen/first_seen")
    repo.upsert_sighting_only(
        channel_id=str(payload["channel_id"]),
        ts=str(payload["ts"]),
        content_sha256=str(payload["content_sha256"]),
        device_id=authenticated_device_id,
        source=str(payload.get("source") or "sync"),
        seen_at=last_seen,
        first_seen=first_seen or last_seen,
        snapshot_id=str(payload.get("snapshot_id") or "sync"),
        state_sha256=str(payload.get("state_sha256") or "sync"),
        commit=False,
        bump_count=False,
    )
    # Merge sighting times into the version row used by projection.
    repo.merge_version_sighting_times(
        channel_id=str(payload["channel_id"]),
        ts=str(payload["ts"]),
        content_sha256=str(payload["content_sha256"]),
        first_seen=first_seen or last_seen,
        last_seen=last_seen,
        commit=False,
    )
