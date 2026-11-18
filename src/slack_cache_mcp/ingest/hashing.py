"""Message content hashing and search text extraction."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

# Excluded from content hash (client-private + volatile thread stats + reactions)
_HASH_EXCLUDE = {
    "reply_count",
    "reply_users",
    "reply_users_count",
    "latest_reply",
    "subscribed",
    "last_read",
    "reactions",
}

_MRKDWN_USER = re.compile(r"<@(U[A-Z0-9]+)(?:\|([^>]+))?>")
_MRKDWN_CHAN = re.compile(r"<#(C[A-Z0-9]+)(?:\|([^>]+))?>")
_MRKDWN_LINK = re.compile(r"<(https?://[^|>]+)\|([^>]+)>")
_MRKDWN_RAW = re.compile(r"<(https?://[^>]+)>")
_MRKDWN_SPECIAL = re.compile(r"<!(here|channel|everyone)>")


def normalize_ts(ts: Any) -> str:
    if ts is None:
        return ""
    if isinstance(ts, float):
        return f"{ts:.6f}"
    s = str(ts)
    if re.fullmatch(r"\d+", s):
        return f"{s}.000000"
    if re.fullmatch(r"\d+\.\d+", s):
        whole, frac = s.split(".", 1)
        return f"{whole}.{frac[:6].ljust(6, '0')}"
    return s


def _canonical_message_fields(msg: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in (
        "text",
        "blocks",
        "attachments",
        "subtype",
        "user",
        "bot_id",
        "thread_ts",
        "edited",
        "client_msg_id",
    ):
        if key in msg:
            out[key] = msg[key]
    for k, v in msg.items():
        if isinstance(k, str) and k.startswith("whisper_"):
            out[k] = v
    files = msg.get("files")
    if isinstance(files, list):
        out["files"] = [{"id": f.get("id")} for f in files if isinstance(f, dict) and f.get("id")]
    # Drop underscore keys
    return {k: v for k, v in out.items() if not str(k).startswith("_") and k not in _HASH_EXCLUDE}


def content_sha256(msg: Mapping[str, Any]) -> str:
    payload = _canonical_message_fields(msg)
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def flatten_mrkdwn(text: str) -> str:
    t = _MRKDWN_USER.sub(lambda m: f"@{m.group(2) or m.group(1)}", text)
    t = _MRKDWN_CHAN.sub(lambda m: f"#{m.group(2) or m.group(1)}", t)
    t = _MRKDWN_LINK.sub(lambda m: f"{m.group(2)} ({m.group(1)})", t)
    t = _MRKDWN_RAW.sub(lambda m: m.group(1), t)
    t = _MRKDWN_SPECIAL.sub(lambda m: f"@{m.group(1)}", t)
    return t


def search_text_from_message(msg: Mapping[str, Any]) -> str:
    parts: list[str] = []
    text = msg.get("text")
    if isinstance(text, str):
        parts.append(flatten_mrkdwn(text))
    # blocks rich_text — shallow flatten
    blocks = msg.get("blocks")
    if isinstance(blocks, list):
        for b in blocks:
            if not isinstance(b, dict):
                continue
            for el in b.get("elements") or []:
                if isinstance(el, dict):
                    for sub in el.get("elements") or []:
                        if isinstance(sub, dict) and isinstance(sub.get("text"), str):
                            parts.append(sub["text"])
    for att in msg.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        for k in ("fallback", "text", "title", "pretext"):
            if isinstance(att.get(k), str):
                parts.append(att[k])
    for f in msg.get("files") or []:
        if not isinstance(f, dict):
            continue
        for k in ("title", "name", "preview", "plain_text"):
            if isinstance(f.get(k), str):
                parts.append(f[k])
    return "\n".join(p for p in parts if p)
