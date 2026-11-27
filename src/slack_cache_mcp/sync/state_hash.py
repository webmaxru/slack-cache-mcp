"""Deterministic archive state hash for multi-device convergence checks."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from slack_cache_mcp.store.sqlite.repo import SqliteRepository


def _msg_proj(row: Any) -> dict[str, Any]:
    return {
        "channel_id": row["channel_id"],
        "ts": row["ts"],
        "workspace_key": row["workspace_key"],
        "user_id": row["user_id"],
        "thread_ts": row["thread_ts"],
        "subtype": row["subtype"],
        "text": row["text"],
        "current_sha256": row["current_sha256"],
        "edited_ts": row["edited_ts"],
        "deleted_state": row["deleted_state"],
        "reply_count": row["reply_count"],
        "latest_reply": row["latest_reply"],
    }


def _channel_proj(row: Any) -> dict[str, Any]:
    return {
        "channel_id": row["channel_id"],
        "workspace_key": row["workspace_key"],
        "name": row["name"],
        "kind": row["kind"],
        "topic": row["topic"],
        "purpose": row["purpose"],
        "is_archived": row["is_archived"],
        "is_member": row["is_member"],
    }


def _user_proj(row: Any) -> dict[str, Any]:
    return {
        "user_id": row["user_id"],
        "name": row["name"],
        "real_name": row["real_name"],
        "display_name": row["display_name"],
        "is_bot": row["is_bot"],
        "is_deleted": row["is_deleted"],
    }


def _workspace_proj(row: Any) -> dict[str, Any]:
    return {
        "workspace_key": row["workspace_key"],
        "team_id": row["team_id"],
        "name": row["name"],
        "domain": row["domain"],
        "self_user_id": row["self_user_id"],
    }


def compute_state_hash(repo: SqliteRepository) -> str:
    """Hash over messages + directory tables (order-independent content).

    PLAN §14: identical ``state_hash`` after random-order / duplicated bundle apply.
    """
    conn = repo._conn  # noqa: SLF001
    payload = {
        "messages": [
            _msg_proj(r)
            for r in conn.execute(
                "SELECT * FROM messages ORDER BY channel_id, ts"
            ).fetchall()
        ],
        "channels": [
            _channel_proj(r)
            for r in conn.execute(
                "SELECT * FROM channels ORDER BY channel_id"
            ).fetchall()
        ],
        "users": [
            _user_proj(r)
            for r in conn.execute("SELECT * FROM users ORDER BY user_id").fetchall()
        ],
        "workspaces": [
            _workspace_proj(r)
            for r in conn.execute(
                "SELECT * FROM workspaces ORDER BY workspace_key"
            ).fetchall()
        ],
        "message_versions": [
            {
                "channel_id": r["channel_id"],
                "ts": r["ts"],
                "content_sha256": r["content_sha256"],
                "edited_ts": r["edited_ts"],
                "is_deletion": r["is_deletion"],
                "is_partial": r["is_partial"],
                "source_rank": r["source_rank"],
                "text": r["text"],
            }
            for r in conn.execute(
                """
                SELECT channel_id, ts, content_sha256, edited_ts, is_deletion,
                       is_partial, source_rank, text
                FROM message_versions
                ORDER BY channel_id, ts, content_sha256
                """
            ).fetchall()
        ],
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
