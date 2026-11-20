"""Local Storage helpers — team names + opt-in drafts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from slack_cache_mcp.desktop.sanitize import parse_local_config_teams, sanitize
from slack_cache_mcp.domain.models import Draft

_DRAFT_KEY_RE = re.compile(
    r"^persist-v1::(?P<team>[TE][A-Z0-9]+)::(?P<user>U[A-Z0-9]+)::drafts?$",
    re.I,
)


def load_local_config_teams(mirror_or_profile: Path) -> dict[str, dict[str, Any]]:
    """Load allowlisted team fields.

    Prefer side-car `Local Storage/localConfig_v2.json` (synthetic fixtures).
    Never return raw localConfig.
    """
    side = Path(mirror_or_profile) / "Local Storage" / "localConfig_v2.json"
    if side.is_file():
        raw = json.loads(side.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        return parse_local_config_teams(raw)
    return {}


def _load_local_entries(mirror: Path) -> list[dict[str, Any]]:
    path = Path(mirror) / "Local Storage" / "local_entries.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def load_drafts(mirror_or_profile: Path) -> list[Draft]:
    """Opt-in: extract draft text from Local Storage side-car entries."""
    out: list[Draft] = []
    for entry in _load_local_entries(Path(mirror_or_profile)):
        key = str(entry.get("key") or "")
        m = _DRAFT_KEY_RE.match(key)
        team = m.group("team") if m else str(entry.get("workspace_key") or "unknown")
        val = entry.get("value")
        drafts_obj: Any = val
        if isinstance(val, str):
            try:
                drafts_obj = json.loads(val)
            except json.JSONDecodeError:
                drafts_obj = {"text": val}
        if isinstance(drafts_obj, dict) and "drafts" in drafts_obj:
            items = drafts_obj["drafts"]
        else:
            items = drafts_obj
        pairs: list[tuple[str, Any]]
        if isinstance(items, dict):
            pairs = [(str(k), v) for k, v in items.items()]
        elif isinstance(items, list):
            pairs = [(str(i), x) for i, x in enumerate(items)]
        else:
            continue
        for did, d in pairs:
            if not isinstance(d, dict):
                continue
            clean = sanitize(d)
            assert isinstance(clean, dict)
            text = clean.get("text") or clean.get("message") or ""
            if not isinstance(text, str) or not text.strip():
                continue
            out.append(
                Draft(
                    workspace_key=team,
                    draft_id=str(clean.get("client_msg_id") or clean.get("id") or did),
                    channel_id=str(clean["channel_id"])
                    if clean.get("channel_id")
                    else (str(clean["channel"]) if clean.get("channel") else None),
                    thread_ts=str(clean["thread_ts"]) if clean.get("thread_ts") else None,
                    text=text,
                    last_updated=float(clean["ts"])
                    if isinstance(clean.get("ts"), (int, float))
                    else None,
                    raw=clean,
                )
            )
    return out


def inventory_local_storage_keys(mirror: Path) -> list[str]:
    """ID-redacted key patterns for doctor (no values)."""
    patterns: list[str] = []
    for entry in _load_local_entries(mirror):
        key = str(entry.get("key") or "")
        redacted = re.sub(r"[TE][A-Z0-9]{8,}", "<ID>", key)
        redacted = re.sub(r"U[A-Z0-9]{8,}", "<UID>", redacted)
        if redacted and redacted not in patterns:
            patterns.append(redacted)
    return patterns[:50]
