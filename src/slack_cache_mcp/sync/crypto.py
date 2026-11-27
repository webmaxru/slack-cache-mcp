"""Age (X25519) encrypt/decrypt via pyrage + Ed25519 envelope signing."""

from __future__ import annotations

import base64
import json
from typing import Any

import zstandard as zstd
from pyrage import decrypt, encrypt, x25519

from slack_cache_mcp.sync.device import DeviceIdentity, verify_ed25519

# v2: signature covers canonical header fields + payload (not payload alone).
BUNDLE_FORMAT_VERSION = 2


def age_encrypt(plaintext: bytes, recipients: list[str]) -> bytes:
    """Encrypt ``plaintext`` to one or more age recipient strings."""
    if not recipients:
        raise ValueError("at least one age recipient is required")
    recs = [x25519.Recipient.from_str(r) for r in recipients]
    out: bytes = encrypt(plaintext, recs)
    return out


def age_decrypt(ciphertext: bytes, identity: x25519.Identity) -> bytes:
    out: bytes = decrypt(ciphertext, [identity])
    return out


def _canonical_signed_bytes(envelope_without_sig: dict[str, Any]) -> bytes:
    """Canonical JSON of every field except the signature itself."""
    return json.dumps(
        envelope_without_sig, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def build_signed_envelope(
    *,
    identity: DeviceIdentity,
    from_seq: int,
    to_seq: int,
    rows: list[dict[str, Any]],
) -> bytes:
    """JSONL rows → zstd → signed envelope JSON bytes (pre-age)."""
    lines = [
        json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        for row in rows
    ]
    raw = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    compressed = zstd.ZstdCompressor(level=3).compress(raw)
    envelope_body = {
        "v": BUNDLE_FORMAT_VERSION,
        "device_id": identity.device_id,
        "from_seq": from_seq,
        "to_seq": to_seq,
        "ed25519_public": identity.ed25519_public_b64,
        "age_recipient": identity.age_recipient,
        "label": identity.label,
        "payload_zstd_b64": base64.b64encode(compressed).decode("ascii"),
        "row_count": len(rows),
    }
    signature = identity.sign(_canonical_signed_bytes(envelope_body))
    envelope = {**envelope_body, "signature_b64": base64.b64encode(signature).decode("ascii")}
    return json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")


def open_signed_envelope(
    envelope_bytes: bytes,
    *,
    require_device_id: str | None = None,
    expected_ed25519_public: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Verify signature (and optional pinned key) and return (header, rows)."""
    try:
        envelope = json.loads(envelope_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid bundle envelope JSON") from exc
    if not isinstance(envelope, dict):
        raise ValueError("bundle envelope must be an object")
    if int(envelope.get("v", -1)) != BUNDLE_FORMAT_VERSION:
        raise ValueError(f"unsupported bundle version: {envelope.get('v')!r}")
    device_id = str(envelope.get("device_id") or "")
    if not device_id:
        raise ValueError("bundle missing device_id")
    if require_device_id and device_id != require_device_id:
        raise ValueError("bundle device_id mismatch")
    pub = str(envelope.get("ed25519_public") or "")
    payload_b64 = str(envelope.get("payload_zstd_b64") or "")
    sig_b64 = str(envelope.get("signature_b64") or "")
    if not pub or not payload_b64 or not sig_b64:
        raise ValueError("bundle missing signature fields")
    if expected_ed25519_public is not None and pub != expected_ed25519_public:
        raise ValueError("bundle signing key does not match registered peer")
    signature = base64.b64decode(sig_b64.encode("ascii"))
    body = {k: v for k, v in envelope.items() if k != "signature_b64"}
    if not verify_ed25519(pub, _canonical_signed_bytes(body), signature):
        raise ValueError("bundle Ed25519 signature verification failed")
    compressed = base64.b64decode(payload_b64.encode("ascii"))
    raw = zstd.ZstdDecompressor().decompress(compressed)
    rows: list[dict[str, Any]] = []
    for line in raw.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if not isinstance(obj, dict):
            raise ValueError("change_log row must be an object")
        rows.append(obj)
    return envelope, rows
