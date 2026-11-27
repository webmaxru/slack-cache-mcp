"""Folder-based age-encrypted bundle export/import (stage 1.5, offline)."""

from __future__ import annotations

import contextlib
import json
import os
import time
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


@contextlib.contextmanager
def _export_dir_lock(to_dir: Path) -> Any:
    """Serialize export/cleanup/cursor updates for one exchange folder."""
    import sys

    lock_path = to_dir / ".slackcache-export.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if not lock_path.exists():
        with contextlib.suppress(OSError):
            lock_path.write_text("0", encoding="utf-8")
    fh = lock_path.open("a+b")
    try:
        while True:
            try:
                fh.seek(0)
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                time.sleep(0.05)
        yield
    finally:
        with contextlib.suppress(OSError):
            fh.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


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

    with _export_dir_lock(to_dir):
        return _export_bundle_locked(repo, identity=identity, to_dir=to_dir, peer=peer)


def _export_bundle_locked(
    repo: SqliteRepository,
    *,
    identity: DeviceIdentity,
    to_dir: Path,
    peer: str,
) -> dict[str, Any]:
    # Ensure upgraded archives have complete outbox facts before export.
    hlc = restore_hlc(repo, identity.device_id)
    backfill = repo.backfill_outbox(device_id=identity.device_id, hlc=hlc.now())
    repo.set_hlc_watermark(hlc.now())

    cursor_raw = repo.get_sync_cursor(peer, "push")
    since_seq = int(cursor_raw) if cursor_raw is not None else 0
    rows = repo.list_change_log_since(device_id=identity.device_id, since_seq=since_seq)
    # Stable dependency order inside the bundle (workspace → channel → …).
    _order = {
        "workspace": 0,
        "channel": 1,
        "user": 2,
        "message_version": 3,
        "sighting": 4,
    }
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

    # Sequence bounds are independent of dependency order (min/max of the batch).
    seqs = [int(r["seq"]) for r in rows]
    from_seq = min(seqs)
    to_seq = max(seqs)
    rows = sorted(
        rows,
        key=lambda r: (_order.get(str(r.get("entity") or ""), 99), int(r.get("seq") or 0)),
    )
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
    # Atomic publish: unique temp then replace so concurrent exporters cannot
    # clobber each other, and a crash cannot truncate an existing bundle.
    tmp_path = to_dir / f".{name}.{os.getpid()}.{time.time_ns()}.tmp"
    tmp_path.write_bytes(ciphertext)
    tmp_path.replace(path)
    if since_seq <= 0:
        for stale in to_dir.glob(f"bundle-{identity.device_id}-*.jsonl.zst.age"):
            if stale.name != name:
                with contextlib.suppress(OSError):
                    stale.unlink()
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
    # Verify all bundles first, then apply rows in global dependency order so a
    # channel file that sorts before its workspace file still succeeds in one pass.
    verified: list[tuple[Path, dict[str, Any], list[dict[str, Any]], str]] = []

    for path in files:
        try:
            ciphertext = path.read_bytes()
            # Decrypt failures are always reported. Late-peer / superseded bundles
            # must be removed from the exchange folder after a full re-export —
            # we never infer identity or coverage from unauthenticated filenames.
            envelope_bytes = age_decrypt(ciphertext, identity.age_identity)
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
            verified.append((path, header, rows, sender_id))
        except Exception as exc:
            errors.append({"path": str(path), "error": str(exc)})

    all_rows: list[dict[str, Any]] = []
    for _path, _header, rows, _sender_id in verified:
        all_rows.extend(rows)
    if all_rows:
        stats = apply_change_rows(repo, all_rows, hlc_clock=hlc_clock)
        total_applied += int(stats["applied"])
        total_skipped += int(stats["skipped"])
        for err in stats.get("errors") or []:
            errors.append({"path": str(from_dir), "error": str(err)})
        for _path, header, _rows, sender_id in verified:
            imported_files += 1
            to_seq = header.get("to_seq")
            if to_seq is not None:
                repo.set_sync_cursor(f"{peer}:{sender_id}", "pull", str(to_seq))

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
