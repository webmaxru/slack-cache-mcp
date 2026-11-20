"""Session Storage inventory + message-shaped JSON scan (low yield)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from slack_cache_mcp.desktop.sanitize import sanitize
from slack_cache_mcp.desktop.slack.redux import is_message, walk_messages
from slack_cache_mcp.domain.models import MessageObservation
from slack_cache_mcp.ingest.hashing import content_sha256, normalize_ts, search_text_from_message


def _load_sidecar(mirror: Path) -> list[dict[str, Any]]:
    """Synthetic fixture: Session Storage/session_entries.json."""
    path = Path(mirror) / "Session Storage" / "session_entries.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


def inventory_session_storage(mirror: Path) -> dict[str, Any]:
    root = Path(mirror) / "Session Storage"
    if not root.exists():
        return {"present": False, "files": 0, "entries": 0}
    files = sum(1 for p in root.rglob("*") if p.is_file() and p.name != "LOCK")
    entries = _load_sidecar(mirror)
    return {
        "present": True,
        "files": files,
        "entries": len(entries),
        "key_patterns": sorted(
            {str(e.get("key", ""))[:40] for e in entries if e.get("key")}
        )[:20],
    }


def scan_session_messages(
    mirror: Path,
    *,
    workspace_key: str = "unknown",
) -> list[MessageObservation]:
    """Extract message-shaped objects from Session Storage side-car values."""
    out: list[MessageObservation] = []
    for entry in _load_sidecar(mirror):
        val = entry.get("value")
        found: dict[tuple[str | None, str], dict[str, Any]] = {}
        walk_messages(val, None, found)
        if isinstance(val, dict) and is_message(val):
            found[(val.get("channel"), normalize_ts(val.get("ts")))] = val
        for (cid, _ts), msg in found.items():
            if not cid:
                continue
            clean = sanitize(dict(msg))
            assert isinstance(clean, dict)
            ts = normalize_ts(clean.get("ts"))
            if not ts:
                continue
            text_val = clean.get("text")
            text = text_val if isinstance(text_val, str) else ""
            out.append(
                MessageObservation(
                    channel_id=str(cid),
                    ts=ts,
                    workspace_key=workspace_key,
                    user_id=str(clean["user"]) if clean.get("user") else None,
                    thread_ts=normalize_ts(clean.get("thread_ts"))
                    if clean.get("thread_ts")
                    else None,
                    subtype=str(clean["subtype"]) if clean.get("subtype") else None,
                    text=text,
                    search_text=search_text_from_message(clean),
                    content_sha256=content_sha256(clean),
                    edited_ts=None,
                    is_deletion=False,
                    is_partial=True,
                    source="session.storage",
                    source_rank=40,
                    raw=clean,
                )
            )
    return out
