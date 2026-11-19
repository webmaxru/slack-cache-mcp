"""Local Storage helpers — team names only from localConfig_v2."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from slack_cache_mcp.desktop.sanitize import parse_local_config_teams


def load_local_config_teams(mirror_or_profile: Path) -> dict[str, dict[str, Any]]:
    """Load allowlisted team fields.

    M1: prefer side-car `Local Storage/localConfig_v2.json` (synthetic fixtures).
    Real LevelDB decoding of localConfig arrives with richer Local Storage support later;
    never return raw localConfig.
    """
    side = Path(mirror_or_profile) / "Local Storage" / "localConfig_v2.json"
    if side.is_file():
        raw = json.loads(side.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        return parse_local_config_teams(raw)
    return {}
