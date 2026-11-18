"""Drop secrets before persistence, vault, logs, or export."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

TOKEN_RE = re.compile(r"xox[a-z]-[A-Za-z0-9%\-_.]+|xapp-[A-Za-z0-9%\-_.]+", re.IGNORECASE)

_SENSITIVE_EXACT = {
    "token",
    "tokens",
    "api_token",
    "xoxc",
    "xoxd",
    "xoxp",
    "cookie",
    "cookies",
    "d",
    "d-s",
    "session",
    "auth",
    "secret",
    "password",
    "refresh_token",
    "access_token",
    "enterprise_api_token",
}

_BOOTDATA_ALLOW = {
    "team_id",
    "user_id",
    "team_url",
    "version_ts",
    "version_uid",
    "app",
    "build_version",
}


def _key_sensitive(key: str) -> bool:
    lk = key.lower()
    if lk in _SENSITIVE_EXACT:
        return True
    if lk.endswith("_token") or lk.endswith("token"):
        return True
    return False


def scrub_string(s: str) -> str:
    return TOKEN_RE.sub("<REDACTED>", s)


def sanitize(value: Any, *, path: tuple[str, ...] = ()) -> Any:
    """Recursively drop sensitive keys and scrub token-shaped strings."""
    if isinstance(value, str):
        return scrub_string(value)
    if isinstance(value, Mapping):
        # bootData allowlist
        if path and path[-1] == "bootData":
            out: dict[str, Any] = {}
            for k, v in value.items():
                if str(k) in _BOOTDATA_ALLOW:
                    out[str(k)] = sanitize(v, path=path + (str(k),))
            return out
        out_m: dict[str, Any] = {}
        for k, v in value.items():
            ks = str(k)
            if _key_sensitive(ks):
                continue
            out_m[ks] = sanitize(v, path=path + (ks,))
        return out_m
    if isinstance(value, list):
        return [sanitize(v, path=path) for v in value]
    if isinstance(value, tuple):
        return tuple(sanitize(v, path=path) for v in value)
    return value


LOCALCONFIG_ALLOW = (
    "id",
    "name",
    "domain",
    "url",
    "user_id",
    "enterprise_id",
    "enterprise_name",
)


def parse_local_config_teams(raw: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Extract allowlisted team fields from localConfig_v2; drop tokens and all else."""
    if not raw:
        return {}
    teams_in = raw.get("teams")
    if not isinstance(teams_in, Mapping):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for tid, team in teams_in.items():
        if not isinstance(team, Mapping):
            continue
        slim: dict[str, Any] = {}
        for k in LOCALCONFIG_ALLOW:
            if k in team:
                val = team[k]
                slim[k] = scrub_string(val) if isinstance(val, str) else val
        if "id" not in slim:
            slim["id"] = str(tid)
        out[str(tid)] = slim
    return out
