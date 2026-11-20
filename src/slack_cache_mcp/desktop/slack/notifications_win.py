"""Opt-in Windows notification DB source (copy then open; never live).

Reads toast XML for Slack AUMIDs. Observations are marked partial (previews truncate).
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import sqlite3
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from slack_cache_mcp.desktop.sanitize import sanitize
from slack_cache_mcp.domain.models import MessageObservation
from slack_cache_mcp.ingest.hashing import content_sha256, normalize_ts, search_text_from_message

_SLACK_AUMID_PATTERNS = (
    re.compile(r"91750D7E\.Slack_", re.I),
    re.compile(r"Slack\.Slack", re.I),
    re.compile(r"^Slack$", re.I),
    re.compile(r"com\.tinyspeck\.slackmacgap", re.I),
)


def looks_like_slack_aumid(aumid: str) -> bool:
    return any(p.search(aumid) for p in _SLACK_AUMID_PATTERNS)


def default_notifications_db_path() -> Path | None:
    """Path under LocalAppData — only used when opt-in AND caller passes/copies it."""
    import os

    la = os.environ.get("LOCALAPPDATA")
    if not la:
        return None
    return Path(la) / "Microsoft" / "Windows" / "Notifications" / "wpndatabase.db"


def copy_notifications_db(src: Path, dest_dir: Path) -> Path | None:
    """Copy db + wal/shm into dest_dir. Return dest db or None."""
    src = Path(src)
    if not src.is_file():
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "wpndatabase.db"
    try:
        shutil.copy2(src, dest)
    except OSError:
        return None
    for suffix in ("-wal", "-shm", "-journal"):
        sib = Path(str(src) + suffix)
        if sib.is_file():
            with contextlib.suppress(OSError):
                shutil.copy2(sib, dest_dir / sib.name)
    return dest


def _text_from_toast_xml(xml_text: str) -> tuple[str | None, str | None, str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None, None, xml_text[:500]
    texts = [n.text.strip() for n in root.iter() if n.text and n.text.strip()]
    body = " | ".join(texts[:4]) if texts else ""
    channel = texts[0] if texts else None
    sender = texts[1] if len(texts) > 1 else None
    return channel, sender, body


def parse_notification_rows(
    rows: list[dict[str, Any]],
    *,
    workspace_key: str = "unknown",
) -> list[MessageObservation]:
    out: list[MessageObservation] = []
    for row in rows:
        aumid = str(row.get("aumid") or row.get("AppId") or "")
        if aumid and not looks_like_slack_aumid(aumid):
            continue
        payload = row.get("payload") or row.get("Payload") or row.get("xml") or ""
        if not isinstance(payload, str):
            payload = str(payload)
        channel_hint, sender, body = _text_from_toast_xml(payload)
        channel_id = str(row.get("channel_id") or channel_hint or "C0NOTIFUNKN")
        ts = normalize_ts(row.get("ts") or row.get("ArrivalTime") or "0")
        if not ts or ts == "0.000000":
            rid = row.get("id") or row.get("NotificationId") or len(out)
            try:
                rid_i = int(rid)
            except (TypeError, ValueError):
                rid_i = len(out)
            ts = f"1763409{rid_i:03d}.000100"
        preview_text = body or str(row.get("text") or "")
        msg = sanitize(
            {
                "type": "message",
                "ts": ts,
                "channel": channel_id,
                "user": row.get("user_id") or sender,
                "text": preview_text,
                "subtype": "notification_preview",
            }
        )
        assert isinstance(msg, dict)
        text_val = msg.get("text")
        text = text_val if isinstance(text_val, str) else preview_text
        out.append(
            MessageObservation(
                channel_id=channel_id,
                ts=ts,
                workspace_key=workspace_key,
                user_id=str(msg["user"]) if msg.get("user") else None,
                thread_ts=None,
                subtype="notification_preview",
                text=text,
                search_text=search_text_from_message(msg),
                content_sha256=content_sha256(msg),
                edited_ts=None,
                is_deletion=False,
                is_partial=True,
                source="notif.win",
                source_rank=30,
                raw=msg,
            )
        )
    return out


def load_from_sidecar(mirror_or_dir: Path) -> list[dict[str, Any]]:
    path = Path(mirror_or_dir) / "notifications_win.json"
    if not path.is_file():
        alt = Path(mirror_or_dir) / "Notifications" / "notifications_win.json"
        path = alt if alt.is_file() else path
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def extract_notifications(
    mirror_or_copy: Path,
    *,
    workspace_key: str = "unknown",
    live_db: Path | None = None,
) -> list[MessageObservation]:
    """Prefer synthetic side-car; optionally copy+scan a provided wpndatabase.db path."""
    rows = load_from_sidecar(mirror_or_copy)
    if rows:
        return parse_notification_rows(rows, workspace_key=workspace_key)

    src = live_db
    if src is None:
        candidate = Path(mirror_or_copy) / "Notifications" / "wpndatabase.db"
        src = candidate if candidate.is_file() else None
    if src is None or not Path(src).is_file():
        return []

    with tempfile.TemporaryDirectory(prefix="slackcache-notif-") as tmp:
        dest = copy_notifications_db(Path(src), Path(tmp))
        if dest is None:
            return []
        try:
            conn = sqlite3.connect(f"file:{dest.as_posix()}?mode=ro&immutable=1", uri=True)
        except sqlite3.Error:
            return []
        try:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
            parsed_rows: list[dict[str, Any]] = []
            for table in tables:
                if "notif" not in table.lower() and table.lower() not in {
                    "notification",
                    "notificationhandler",
                }:
                    continue
                try:
                    cur = conn.execute(f"SELECT * FROM {table} LIMIT 200")
                    cols = [d[0] for d in cur.description]
                    for row in cur.fetchall():
                        parsed_rows.append(dict(zip(cols, row, strict=False)))
                except sqlite3.Error:
                    continue
            return parse_notification_rows(parsed_rows, workspace_key=workspace_key)
        finally:
            conn.close()
