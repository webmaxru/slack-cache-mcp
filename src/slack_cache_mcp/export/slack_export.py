"""Write Slack-export-compatible directory layout (format compatibility only; no AGPL code).

Layout: channels.json, groups.json, dms.json, mpims.json, users.json, <channel>/<YYYY-MM-DD>.json
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from slack_cache_mcp.desktop.sanitize import sanitize
from slack_cache_mcp.store.sqlite.repo import SqliteRepository


def _day_key(ts: str) -> str:
    try:
        secs = float(ts)
    except ValueError:
        return "1970-01-01"
    return datetime.fromtimestamp(secs, tz=UTC).strftime("%Y-%m-%d")


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _channel_id(ch: dict[str, Any]) -> Any:
    # SqliteRepository.list_channels() provides per-channel dictionaries keyed with "id".
    # Fall back to "channel_id" for compatibility with any older/alternate schemas.
    return ch.get("id") or ch.get("channel_id")


def _channel_export_row(ch: dict[str, Any]) -> dict[str, Any]:
    cid = _channel_id(ch)
    return _as_dict(
        sanitize(
            {
                "id": cid,
                "name": ch.get("name") or cid,
                "created": 0,
                "creator": "",
                "is_archived": bool(ch.get("is_archived")),
                "is_general": ch.get("name") == "general",
                "members": [],
                "topic": {"value": ch.get("topic") or "", "creator": "", "last_set": 0},
                "purpose": {"value": ch.get("purpose") or "", "creator": "", "last_set": 0},
            }
        )
    )


def _user_export_row(u: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(
        sanitize(
            {
                "id": u.get("user_id"),
                "team_id": u.get("team_id"),
                "name": u.get("name"),
                "deleted": bool(u.get("is_deleted")),
                "real_name": u.get("real_name"),
                "profile": {
                    "display_name": u.get("display_name"),
                    "real_name": u.get("real_name"),
                    "title": u.get("title"),
                },
                "is_bot": bool(u.get("is_bot")),
                "is_restricted": bool(u.get("is_restricted")),
                "is_ultra_restricted": bool(u.get("is_ultra_restricted")),
            }
        )
    )


def _message_export_row(m: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "type": "message",
        "user": m.get("user_id"),
        "text": m.get("text") or "",
        "ts": m.get("ts"),
    }
    if m.get("thread_ts"):
        row["thread_ts"] = m["thread_ts"]
    if m.get("subtype"):
        row["subtype"] = m["subtype"]
    if m.get("reply_count"):
        row["reply_count"] = m["reply_count"]
    return _as_dict(sanitize(row))


def write_slack_export(
    repo: SqliteRepository,
    out_dir: Path,
    *,
    workspace: str | None = None,
) -> dict[str, Any]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    channels = repo.list_channels(workspace=workspace, limit=10_000)
    users = []
    # list_users may not exist — query directly
    try:
        users = [
            dict(r)
            for r in repo._conn.execute(  # noqa: SLF001
                "SELECT * FROM users ORDER BY user_id"
            ).fetchall()
        ]
    except Exception:  # noqa: BLE001
        users = []

    public: list[dict[str, Any]] = []
    private: list[dict[str, Any]] = []
    dms: list[dict[str, Any]] = []
    mpims: list[dict[str, Any]] = []
    for ch in channels:
        kind = ch.get("kind")
        row = _channel_export_row(ch)
        if kind == "public":
            public.append(row)
        elif kind == "private":
            private.append(row)
        elif kind == "im":
            dms.append(
                sanitize(
                    {
                        "id": ch.get("channel_id"),
                        "created": 0,
                        "members": [ch.get("im_user_id")] if ch.get("im_user_id") else [],
                    }
                )
            )
        elif kind == "mpim":
            mpims.append(row)
        else:
            public.append(row)

    (out_dir / "channels.json").write_text(
        json.dumps(public, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (out_dir / "groups.json").write_text(
        json.dumps(private, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (out_dir / "dms.json").write_text(
        json.dumps(dms, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (out_dir / "mpims.json").write_text(
        json.dumps(mpims, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (out_dir / "users.json").write_text(
        json.dumps([_user_export_row(u) for u in users], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    messages = repo.list_messages_for_export(workspace=workspace)
    by_channel_day: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for m in messages:
        cid = str(m.get("channel_id"))
        day = _day_key(str(m.get("ts") or "0"))
        by_channel_day[cid][day].append(_message_export_row(m))

    files_written = 0
    for cid, days in by_channel_day.items():
        cdir = out_dir / cid
        cdir.mkdir(parents=True, exist_ok=True)
        for day, rows in days.items():
            (cdir / f"{day}.json").write_text(
                json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            files_written += 1

    return {
        "ok": True,
        "out": str(out_dir),
        "channels": len(public),
        "groups": len(private),
        "dms": len(dms),
        "mpims": len(mpims),
        "users": len(users),
        "message_files": files_written,
        "messages": len(messages),
    }


def write_jsonl_export(
    repo: SqliteRepository,
    out_dir: Path,
    *,
    workspace: str | None = None,
) -> dict[str, Any]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "messages.jsonl"
    n = 0
    with path.open("w", encoding="utf-8") as fh:
        for m in repo.list_messages_for_export(workspace=workspace, include_deleted=True):
            fh.write(json.dumps(sanitize(dict(m)), ensure_ascii=False) + "\n")
            n += 1
    return {"ok": True, "out": str(path), "messages": n}
