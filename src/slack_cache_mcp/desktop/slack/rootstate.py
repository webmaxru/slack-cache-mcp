"""Parse storage/root-state.json for workspace inventory (no secrets)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from slack_cache_mcp.desktop.sanitize import sanitize


def load_root_workspaces(mirror_or_profile: Path) -> dict[str, dict[str, Any]]:
    path = Path(mirror_or_profile) / "storage" / "root-state.json"
    if not path.is_file():
        # snapshot may flatten name in older probe layout; accept either
        alt = Path(mirror_or_profile) / "root-state.json"
        path = alt if alt.is_file() else path
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return {}
    clean = sanitize(raw)
    assert isinstance(clean, dict)
    ws = clean.get("workspaces") or clean.get("teams") or {}
    if not isinstance(ws, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for k, v in ws.items():
        if not isinstance(v, dict):
            continue
        out[str(k)] = {
            kk: v.get(kk)
            for kk in ("name", "domain", "id", "teamId")
            if kk in v
        }
        out[str(k)].setdefault("id", str(k))
    return out
