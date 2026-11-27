"""Folder-based age-encrypted bundle export/import (stage 1.5, offline)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from slack_cache_mcp.ingest.hlc import HLC
from slack_cache_mcp.store.sqlite.repo import SqliteRepository
from slack_cache_mcp.sync.apply import apply_change_rows
from slack_cache_mcp.sync.crypto import (
    age_decrypt,
    age_encrypt,
    build_signed_envelope,
    open_signed_envelope,
)
from slack_cache_mcp.sync.device import (
    DeviceIdentity,
    DevicePublicInfo,
    load_or_create_device,
    parse_public_key_json,
)


def ensure_local_device(repo: SqliteRepository, data_dir: Path) -> DeviceIdentity:
    identity = load_or_create_device(data_dir)
    repo.upsert_device(
        identity.device_id,
        identity.label,
        public_key=identity.public_key_json(),
    )
    return identity


def register_peer(repo: SqliteRepository, peer: DevicePublicInfo) -> None:
    public_key = json.dumps(
        {"ed25519": peer.ed25519_public_b64, "age": peer.age_recipient},
        sort_keys=True,
    )
    repo.upsert_device(peer.device_id, peer.label, public_key=public_key)


def list_age_recipients(
    repo: SqliteRepository, *, include_device_ids: list[str] | None = None
) -> list[str]:
    recipients: list[str] = []
    for row in repo.list_devices():
        if include_device_ids is not None and row["device_id"] not in include_device_ids:
            continue
        keys = parse_public_key_json(row.get("public_key"))
        age = keys.get("age")
        if age:
            recipients.append(age)
    return recipients


def export_bundle(
    repo: SqliteRepository,
    *,
    identity: DeviceIdentity,
    to_dir: Path,
    peer: str = "folder",
) -> dict[str, Any]:
    """Export local change_log rows since push cursor as an age-encrypted bundle."""
    to_dir = Path(to_dir)
    to_dir.mkdir(parents=True, exist_ok=True)

    cursor_raw = repo.get_sync_cursor(peer, "push")
    since_seq = int(cursor_raw) if cursor_raw is not None else 0
    rows = repo.list_change_log_since(device_id=identity.device_id, since_seq=since_seq)
    if not rows:
        return {
            "ok": True,
            "exported": 0,
            "path": None,
            "from_seq": since_seq,
            "to_seq": since_seq,
            "peer": peer,
        }

    from_seq = int(rows[0]["seq"])
    to_seq = int(rows[-1]["seq"])
    serializable: list[dict[str, Any]] = []
    for r in rows:
        payload = r["payload_json"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        serializable.append(
            {
                "seq": r["seq"],
                "hlc": r["hlc"],
                "device_id": r["device_id"],
                "entity": r["entity"],
                "entity_key": r["entity_key"],
                "op": r["op"],
                "payload_json": payload,
                "payload_sha256": r["payload_sha256"],
            }
        )

    recipients = list_age_recipients(repo)
    if identity.age_recipient not in recipients:
        recipients.append(identity.age_recipient)

    envelope = build_signed_envelope(
        identity=identity,
        from_seq=from_seq,
        to_seq=to_seq,
        rows=serializable,
    )
    ciphertext = age_encrypt(envelope, recipients)
    name = f"bundle-{identity.device_id}-{from_seq}-{to_seq}.jsonl.zst.age"
    path = to_dir / name
    path.write_bytes(ciphertext)
    repo.set_sync_cursor(peer, "push", str(to_seq))
    return {
        "ok": True,
        "exported": len(rows),
        "path": str(path),
        "from_seq": from_seq,
        "to_seq": to_seq,
        "peer": peer,
        "recipients": len(recipients),
    }


def import_bundles(
    repo: SqliteRepository,
    *,
    identity: DeviceIdentity,
    from_dir: Path,
    peer: str = "folder",
) -> dict[str, Any]:
    """Decrypt, verify, and apply all ``*.jsonl.zst.age`` bundles in ``from_dir``."""
    from_dir = Path(from_dir)
    if not from_dir.is_dir():
        raise FileNotFoundError(from_dir)

    hlc_clock = HLC(device_id=identity.device_id)
    last = repo.max_local_hlc(identity.device_id)
    if last:
        hlc_clock.observe(last)

    files = sorted(from_dir.glob("bundle-*.jsonl.zst.age"))
    imported_files = 0
    total_applied = 0
    total_skipped = 0
    errors: list[dict[str, str]] = []

    for path in files:
        try:
            ciphertext = path.read_bytes()
            envelope_bytes = age_decrypt(ciphertext, identity.age_identity)
            header, rows = open_signed_envelope(envelope_bytes)
            sender_id = str(header["device_id"])
            if sender_id == identity.device_id:
                continue
            age_recipient = header.get("age_recipient")
            if not age_recipient:
                raise ValueError("bundle header missing age_recipient")
            register_peer(
                repo,
                DevicePublicInfo(
                    device_id=sender_id,
                    label=str(header.get("label") or sender_id),
                    ed25519_public_b64=str(header["ed25519_public"]),
                    age_recipient=str(age_recipient),
                ),
            )
            stats = apply_change_rows(repo, rows, hlc_clock=hlc_clock)
            total_applied += int(stats["applied"])
            total_skipped += int(stats["skipped"])
            imported_files += 1
            to_seq = header.get("to_seq")
            if to_seq is not None:
                repo.set_sync_cursor(f"{peer}:{sender_id}", "pull", str(to_seq))
        except Exception as exc:
            errors.append({"path": str(path), "error": str(exc)})

    return {
        "ok": not errors,
        "files_seen": len(files),
        "files_imported": imported_files,
        "applied": total_applied,
        "skipped": total_skipped,
        "errors": errors,
    }
