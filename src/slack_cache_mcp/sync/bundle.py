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
    """Explicit trust operation: register/replace a peer's public keys.

    Resets the folder push cursor so the next export re-sends history encrypted
    to the updated recipient set (late peers cannot decrypt older bundles).
    """
    public_key = json.dumps(
        {"ed25519": peer.ed25519_public_b64, "age": peer.age_recipient},
        sort_keys=True,
    )
    repo.upsert_device(peer.device_id, peer.label, public_key=public_key)
    # Recipient set changed: any push cursor may omit history the new peer needs,
    # and older bundles omit their age recipient — force full re-export everywhere.
    repo.reset_push_cursors()


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


def restore_hlc(repo: SqliteRepository, device_id: str) -> HLC:
    """Build an HLC that dominates both local outbox and persisted watermark."""
    clock = HLC(device_id=device_id)
    for candidate in (repo.get_hlc_watermark(), repo.max_local_hlc(device_id)):
        if candidate:
            clock.observe(candidate)
    return clock


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

    # Ensure upgraded archives have complete outbox facts before export.
    hlc = restore_hlc(repo, identity.device_id)
    backfill = repo.backfill_outbox(device_id=identity.device_id, hlc=hlc.now())
    repo.set_hlc_watermark(hlc.now())

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
            "backfill": backfill,
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
        "backfill": backfill,
    }


def import_bundles(
    repo: SqliteRepository,
    *,
    identity: DeviceIdentity,
    from_dir: Path,
    peer: str = "folder",
) -> dict[str, Any]:
    """Decrypt, verify against registered peers, and apply bundles."""
    from_dir = Path(from_dir)
    if not from_dir.is_dir():
        raise FileNotFoundError(from_dir)

    hlc_clock = restore_hlc(repo, identity.device_id)

    files = sorted(from_dir.glob("bundle-*.jsonl.zst.age"))
    imported_files = 0
    total_applied = 0
    total_skipped = 0
    errors: list[dict[str, str]] = []
    # sender_id -> max authenticated to_seq imported this pass
    covered_to: dict[str, int] = {}
    pending_decrypt: list[tuple[Path, str, int | None, int | None]] = []

    def _parse_bundle_name(name: str) -> tuple[str | None, int | None, int | None]:
        # bundle-<device>-<from>-<to>.jsonl.zst.age
        stem = name
        for suffix in (".jsonl.zst.age", ".age"):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
        parts = stem.split("-")
        if len(parts) < 4 or parts[0] != "bundle":
            return None, None, None
        try:
            to_seq = int(parts[-1])
            from_seq = int(parts[-2])
        except ValueError:
            return None, None, None
        device = "-".join(parts[1:-2])
        return device or None, from_seq, to_seq

    for path in files:
        try:
            ciphertext = path.read_bytes()
            try:
                envelope_bytes = age_decrypt(ciphertext, identity.age_identity)
            except Exception as dec_exc:
                sender_guess, from_seq, to_seq = _parse_bundle_name(path.name)
                pending_decrypt.append((path, str(dec_exc), from_seq, to_seq))
                if sender_guess:
                    # stash guess on the path via parallel list already
                    pending_decrypt[-1] = (
                        path,
                        f"{dec_exc}|sender_guess={sender_guess}",
                        from_seq,
                        to_seq,
                    )
                continue
            # Peek device_id before full verify so we can pin the registered key.
            peek = json.loads(envelope_bytes.decode("utf-8"))
            if not isinstance(peek, dict):
                raise ValueError("invalid envelope")
            sender_id = str(peek.get("device_id") or "")
            if not sender_id:
                raise ValueError("bundle missing device_id")
            if sender_id == identity.device_id:
                continue
            registered = repo.get_device(sender_id)
            if registered is None or not registered.get("public_key"):
                raise ValueError(
                    f"unknown sender {sender_id}; run `slackcache sync peers add` first"
                )
            keys = parse_public_key_json(registered.get("public_key"))
            expected_ed = keys.get("ed25519")
            expected_age = keys.get("age")
            if not expected_ed or not expected_age:
                raise ValueError(f"peer {sender_id} missing public keys")
            header, rows = open_signed_envelope(
                envelope_bytes,
                expected_ed25519_public=expected_ed,
            )
            if str(header.get("age_recipient") or "") != expected_age:
                raise ValueError("bundle age_recipient does not match registered peer")
            # Do NOT overwrite registered keys from the header (impersonation defense).
            for row in rows:
                if str(row.get("device_id") or "") != sender_id:
                    raise ValueError(
                        "change_log row device_id does not match authenticated sender"
                    )
            stats = apply_change_rows(repo, rows, hlc_clock=hlc_clock)
            total_applied += int(stats["applied"])
            total_skipped += int(stats["skipped"])
            imported_files += 1
            to_seq = header.get("to_seq")
            if to_seq is not None:
                to_i = int(to_seq)
                covered_to[sender_id] = max(covered_to.get(sender_id, 0), to_i)
                repo.set_sync_cursor(f"{peer}:{sender_id}", "pull", str(to_seq))
        except Exception as exc:
            errors.append({"path": str(path), "error": str(exc)})

    # Decrypt failures are superseded only when an authenticated replacement from the
    # same sender covers their to_seq; otherwise they remain integrity errors.
    for path, msg, from_seq, to_seq in pending_decrypt:
        sender_guess = None
        if "|sender_guess=" in msg:
            msg, _, sender_guess = msg.partition("|sender_guess=")
        name_device, name_from, name_to = _parse_bundle_name(path.name)
        sender = sender_guess or name_device
        end = to_seq if to_seq is not None else name_to
        _ = from_seq if from_seq is not None else name_from
        if (
            sender
            and end is not None
            and covered_to.get(sender, -1) >= int(end)
        ):
            total_skipped += 1
            continue
        errors.append(
            {
                "path": str(path),
                "error": f"bundle decrypt failed without replacement coverage: {msg}",
            }
        )

    # Persist merged HLC so subsequent local ingest dominates imported clocks.
    tick = hlc_clock.now()
    repo.set_hlc_watermark(tick)

    return {
        "ok": not errors,
        "files_seen": len(files),
        "files_imported": imported_files,
        "applied": total_applied,
        "skipped": total_skipped,
        "errors": errors,
        "hlc_watermark": tick,
    }
