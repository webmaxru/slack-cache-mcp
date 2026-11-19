"""Extract domain records from a sanitized Redux persist state.

Portions derived from openclaw/slacrawl (MIT) message-detection heuristics.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from slack_cache_mcp.desktop.sanitize import sanitize
from slack_cache_mcp.domain.models import Channel, FileMeta, MessageObservation, User, Workspace
from slack_cache_mcp.ingest.hashing import content_sha256, normalize_ts, search_text_from_message

SOURCE_RANK = {
    "redux.messages": 100,
    "redux.threads": 90,
}


def as_dict(v: Any) -> dict[str, Any]:
    if isinstance(v, dict):
        return v
    if isinstance(v, list):
        return {str(i): x for i, x in enumerate(v)}
    return {}


def is_message(obj: Any) -> bool:
    """Portions derived from openclaw/slacrawl (MIT) looksLikeMessage rules."""
    if not isinstance(obj, dict) or "ts" not in obj:
        return False
    return (
        obj.get("type") == "message"
        or isinstance(obj.get("text"), str)
        or "subtype" in obj
        or "user" in obj
        or "reply_count" in obj
    )


def walk_messages(
    node: Any,
    channel: str | None,
    found: dict[tuple[str | None, str], dict[str, Any]],
    *,
    depth: int = 0,
    seen: set[int] | None = None,
) -> None:
    if seen is None:
        seen = set()
    if depth > 8 or id(node) in seen:
        return
    if isinstance(node, (dict, list)):
        seen.add(id(node))
    if isinstance(node, dict):
        if is_message(node):
            c = node.get("channel") or channel
            found[(c, normalize_ts(node.get("ts")))] = node
        for k, v in node.items():
            if k in ("attachments", "blocks", "files", "metadata"):
                continue
            nc = (
                k
                if isinstance(k, str) and k[:1] in "CDG" and len(k) >= 9
                else channel
            )
            walk_messages(v, nc, found, depth=depth + 1, seen=seen)
    elif isinstance(node, list):
        for v in node:
            walk_messages(v, channel, found, depth=depth + 1, seen=seen)


def channel_kind(c: Mapping[str, Any]) -> str:
    if c.get("is_im"):
        return "im"
    if c.get("is_mpim"):
        return "mpim"
    if c.get("is_private") or c.get("is_group"):
        return "private"
    if c.get("is_channel"):
        return "public"
    return "unknown"


def channel_name(c: Mapping[str, Any], kind: str) -> str | None:
    if kind in ("public", "private", "mpim"):
        n = c.get("name")
        return str(n) if n else None
    return None


def identify_workspace(state: Mapping[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Return (workspace_key_hint, team_id, self_user_id)."""
    sti = as_dict(state.get("selfTeamIds"))
    boot = as_dict(state.get("bootData"))
    team_id = sti.get("teamId") or sti.get("defaultWorkspaceId") or boot.get("team_id")
    user_id = boot.get("user_id")
    return (str(team_id) if team_id else None, str(team_id) if team_id else None, str(user_id) if user_id else None)


def _topic_purpose(val: Any) -> str | None:
    if isinstance(val, dict):
        v = val.get("value")
        return str(v) if v is not None else None
    if isinstance(val, str):
        return val
    return None


def extract_workspace(
    state: Mapping[str, Any],
    *,
    persist_id: str | None = None,
) -> Workspace:
    clean = sanitize(dict(state))
    assert isinstance(clean, dict)
    wk_hint, team_id, user_id = identify_workspace(clean)
    workspace_key = persist_id or wk_hint or team_id or "unknown"
    teams = as_dict(clean.get("teams"))
    team_obj = as_dict(teams.get(team_id)) if team_id else {}
    members = as_dict(clean.get("members"))
    me = as_dict(members.get(user_id)) if user_id else {}
    guest = bool(me.get("is_restricted") or me.get("is_ultra_restricted"))
    enterprise_id = None
    if workspace_key.startswith("E"):
        enterprise_id = workspace_key
    return Workspace(
        workspace_key=workspace_key,
        team_id=team_id,
        enterprise_id=enterprise_id,
        name=team_obj.get("name"),
        domain=team_obj.get("domain"),
        self_user_id=user_id,
        self_is_guest=guest,
        raw={"team_id": team_id, "name": team_obj.get("name"), "domain": team_obj.get("domain")},
    )


def extract_channels(state: Mapping[str, Any], workspace_key: str) -> list[Channel]:
    clean = sanitize(dict(state))
    assert isinstance(clean, dict)
    out: list[Channel] = []
    for cid, c in as_dict(clean.get("channels")).items():
        if not isinstance(c, dict):
            continue
        kind = channel_kind(c)
        out.append(
            Channel(
                channel_id=str(cid),
                workspace_key=workspace_key,
                context_team_id=str(c["context_team_id"]) if c.get("context_team_id") else None,
                name=channel_name(c, kind),
                kind=kind,
                is_ext_shared=bool(c.get("is_ext_shared") or c.get("is_shared")),
                is_archived=bool(c.get("is_archived")),
                is_member=bool(c.get("is_member")),
                topic=_topic_purpose(c.get("topic")),
                purpose=_topic_purpose(c.get("purpose")),
                im_user_id=str(c["user"]) if kind == "im" and c.get("user") else None,
                raw=sanitize(c) if isinstance(c, dict) else {},
            )
        )
    return out


def extract_users(state: Mapping[str, Any], workspace_key: str) -> list[User]:
    clean = sanitize(dict(state))
    assert isinstance(clean, dict)
    out: list[User] = []
    for uid, m in as_dict(clean.get("members")).items():
        if not isinstance(m, dict):
            continue
        profile = as_dict(m.get("profile"))
        out.append(
            User(
                user_id=str(uid),
                team_id=str(m["team_id"]) if m.get("team_id") else None,
                name=m.get("name"),
                real_name=m.get("real_name"),
                display_name=profile.get("display_name") or m.get("name"),
                title=profile.get("title"),
                email=None,  # never prefer email in M1 unless already present — drop for privacy default
                is_bot=bool(m.get("is_bot")),
                is_deleted=bool(m.get("deleted")),
                is_restricted=bool(m.get("is_restricted")),
                is_ultra_restricted=bool(m.get("is_ultra_restricted")),
                is_external=bool(m.get("is_external") or m.get("is_stranger")),
                tz=m.get("tz"),
                raw=sanitize(m) if isinstance(m, dict) else {},
                workspace_key=workspace_key,
            )
        )
    return out


def extract_files(state: Mapping[str, Any], workspace_key: str) -> list[FileMeta]:
    clean = sanitize(dict(state))
    assert isinstance(clean, dict)
    out: list[FileMeta] = []
    for fid, f in as_dict(clean.get("files")).items():
        if not isinstance(f, dict):
            continue
        out.append(
            FileMeta(
                file_id=str(fid),
                workspace_key=workspace_key,
                user_id=str(f["user"]) if f.get("user") else None,
                name=f.get("name"),
                title=f.get("title"),
                mimetype=f.get("mimetype"),
                filetype=f.get("filetype"),
                size=int(f["size"]) if isinstance(f.get("size"), (int, float)) else None,
                preview_text=f.get("preview") or f.get("plain_text"),
                raw=sanitize(f) if isinstance(f, dict) else {},
            )
        )
    return out


def _obs_from_msg(
    msg: Mapping[str, Any],
    *,
    channel_id: str,
    workspace_key: str,
    source: str,
) -> MessageObservation | None:
    ts = normalize_ts(msg.get("ts"))
    if not ts or not channel_id:
        return None
    clean = sanitize(dict(msg))
    assert isinstance(clean, dict)
    subtype = clean.get("subtype")
    is_del = subtype in {"tombstone", "message_deleted"} or (
        bool(clean.get("hidden")) and bool(clean.get("deleted_ts"))
    )
    text_val = clean.get("text")
    text = text_val if isinstance(text_val, str) else ""
    files_val = clean.get("files")
    files = files_val if isinstance(files_val, list) else []
    file_ids = [str(f["id"]) for f in files if isinstance(f, dict) and f.get("id")]
    edited = clean.get("edited") if isinstance(clean.get("edited"), dict) else None
    edited_ts = normalize_ts(edited.get("ts")) if edited else None
    thread_ts = normalize_ts(clean.get("thread_ts")) if clean.get("thread_ts") else None
    reply_users_raw = clean.get("reply_users")
    reply_users_list: list[str] = []
    if isinstance(reply_users_raw, list):
        reply_users_list = [str(u) for u in reply_users_raw if u]
    return MessageObservation(
        channel_id=channel_id,
        ts=ts,
        workspace_key=workspace_key,
        user_id=str(clean["user"]) if clean.get("user") else None,
        thread_ts=thread_ts if thread_ts and thread_ts != ts else thread_ts,
        subtype=str(subtype) if subtype else None,
        text=text,
        search_text=search_text_from_message(clean),
        content_sha256=content_sha256(clean),
        edited_ts=edited_ts,
        is_deletion=is_del,
        is_partial=False,
        source=source,
        source_rank=SOURCE_RANK.get(source, 50),
        raw=clean,
        reply_count=(
            int(clean["reply_count"])
            if isinstance(clean.get("reply_count"), (int, float))
            else None
        ),
        latest_reply=(
            normalize_ts(clean.get("latest_reply")) if clean.get("latest_reply") else None
        ),
        reply_users=reply_users_list,
        file_ids=file_ids,
    )


def extract_messages(state: Mapping[str, Any], workspace_key: str) -> list[MessageObservation]:
    clean = sanitize(dict(state))
    assert isinstance(clean, dict)
    out: list[MessageObservation] = []
    msgs = as_dict(clean.get("messages"))
    for cid, bucket in msgs.items():
        found: dict[tuple[str | None, str], dict[str, Any]] = {}
        walk_messages(bucket, str(cid), found)
        for (c, _ts), msg in found.items():
            channel_id = str(c or cid)
            obs = _obs_from_msg(
                msg,
                channel_id=channel_id,
                workspace_key=workspace_key,
                source="redux.messages",
            )
            if obs:
                out.append(obs)

    threads = clean.get("threads")
    if threads is not None:
        found_t: dict[tuple[str | None, str], dict[str, Any]] = {}
        walk_messages(threads, None, found_t)
        for (c, _ts), msg in found_t.items():
            if not c:
                continue
            obs = _obs_from_msg(
                msg,
                channel_id=str(c),
                workspace_key=workspace_key,
                source="redux.threads",
            )
            if obs:
                out.append(obs)
    return out


_PERSIST_KEY_RE = re.compile(
    r"^persist:slack-client-(?P<id>[TE][A-Z0-9]+)-(?P<user>U[A-Z0-9]+)$"
)


def parse_persist_key(key: str) -> tuple[str | None, str | None]:
    m = _PERSIST_KEY_RE.match(key)
    if not m:
        return None, None
    return m.group("id"), m.group("user")


def extract_all(
    state: Mapping[str, Any],
    *,
    persist_key: str | None = None,
) -> tuple[Workspace, list[Channel], list[User], list[FileMeta], list[MessageObservation]]:
    persist_id, persist_user = parse_persist_key(persist_key) if persist_key else (None, None)
    ws = extract_workspace(state, persist_id=persist_id)
    if persist_user and not ws.self_user_id:
        ws.self_user_id = persist_user
    return (
        ws,
        extract_channels(state, ws.workspace_key),
        extract_users(state, ws.workspace_key),
        extract_files(state, ws.workspace_key),
        extract_messages(state, ws.workspace_key),
    )
