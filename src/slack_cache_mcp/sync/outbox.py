"""Change-log outbox helpers (local seq watermarks / sync_cursors)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from slack_cache_mcp.desktop.sanitize import sanitize


def payload_sha256(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def sanitized_payload(payload: dict[str, Any]) -> dict[str, Any]:
    clean = sanitize(payload)
    if not isinstance(clean, dict):
        raise TypeError("sanitized payload must be a dict")
    return clean


def change_row_dict(
    *,
    seq: int,
    hlc: str,
    device_id: str,
    entity: str,
    entity_key: str,
    op: str,
    payload: dict[str, Any],
    payload_sha: str | None = None,
) -> dict[str, Any]:
    clean = sanitized_payload(payload)
    sha = payload_sha or payload_sha256(clean)
    return {
        "seq": seq,
        "hlc": hlc,
        "device_id": device_id,
        "entity": entity,
        "entity_key": entity_key,
        "op": op,
        "payload_json": clean,
        "payload_sha256": sha,
    }
