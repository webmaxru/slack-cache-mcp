"""Extract domain records from a sanitized Redux persist state.

Portions derived from openclaw/slacrawl (MIT) message-detection heuristics.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from slack_cache_mcp.desktop.sanitize import sanitize
from slack_cache_mcp.domain.models import (
    Channel,
    FileMeta,
    MessageObservation,
    ReactionSet,
    SavedItem,
    User,
    Workspace,
)
from slack_cache_mcp.ingest.hashing import content_sha256, normalize_ts, search_text_from_message

# PLAN §8.3 source ranks
SOURCE_RANK = {
    "redux.messages": 100,
    "redux.threads": 90,
    "redux.allThreads": 90,
    "redux.threadSub": 90,
    "redux.threadMetadata": 90,
    "idb.leveldb.historical": 80,
    "idb.leveldb": 80,
    "idb.sidecar": 75,
    "redux.searchResults": 70,
    "redux.pins": 65,
    "redux.saved": 60,
    "redux.stars": 60,
    "redux.lists": 55,
    "redux.summaryDetail": 50,
    "redux.codeChannelAiSummary": 50,
    "session.storage": 40,
    "notif.win": 30,
    "redux.channelLatests": 20,
}

KNOWN_SLICES = frozenset(
    {
        "channels",
        "members",
        "messages",
        "threads",
        "allThreads",
        "threadSub",
        "threadMetadata",
        "channelHistory",
        "channelCursors",
        "channelLatests",
        "messagesMeta",
        "files",
        "saved",
        "stars",
        "pins",
        "reactions",
        "lists",
        "searchResults",
        "summaryDetail",
        "codeChannelAiSummary",
        "bots",
        "userGroups",
        "teams",
        "bootData",
        "selfTeamIds",
        "channelSections",
        "unreadCounts",
        "workObjects",
        "userAlerts",
        "externalTeamIds",
    }
)


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


def observation_from_message(
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


def _extract_slice_messages(
    node: Any,
    workspace_key: str,
    source: str,
    *,
    default_channel: str | None = None,
    is_partial: bool = False,
) -> list[MessageObservation]:
    found: dict[tuple[str | None, str], dict[str, Any]] = {}
    walk_messages(node, default_channel, found)
    out: list[MessageObservation] = []
    for (c, _ts), msg in found.items():
        if not c and not default_channel:
            continue
        obs = observation_from_message(
            msg,
            channel_id=str(c or default_channel),
            workspace_key=workspace_key,
            source=source,
        )
        if obs:
            if is_partial:
                obs.is_partial = True
                obs.source_rank = SOURCE_RANK.get(source, obs.source_rank)
            out.append(obs)
    return out


def extract_messages(state: Mapping[str, Any], workspace_key: str) -> list[MessageObservation]:
    clean = sanitize(dict(state))
    assert isinstance(clean, dict)
    out: list[MessageObservation] = []
    msgs = as_dict(clean.get("messages"))
    for cid, bucket in msgs.items():
        out.extend(
            _extract_slice_messages(
                bucket, workspace_key, "redux.messages", default_channel=str(cid)
            )
        )

    for slice_name in ("threads", "allThreads", "threadSub", "threadMetadata"):
        node = clean.get(slice_name)
        if node is not None:
            out.extend(
                _extract_slice_messages(
                    node, workspace_key, f"redux.{slice_name}"
                )
            )

    # searchResults: messages from in-app searches (may be outside cache window)
    sr = clean.get("searchResults")
    if sr is not None:
        out.extend(
            _extract_slice_messages(sr, workspace_key, "redux.searchResults")
        )

    # pins / saved / stars may embed message objects
    for slice_name in ("pins", "saved", "stars"):
        node = clean.get(slice_name)
        if node is not None:
            out.extend(
                _extract_slice_messages(node, workspace_key, f"redux.{slice_name}")
            )

    # lists items
    lists = clean.get("lists")
    if lists is not None:
        out.extend(_extract_slice_messages(lists, workspace_key, "redux.lists"))

    # AI summaries — treat summary text as partial message-like when shaped
    for slice_name in ("summaryDetail", "codeChannelAiSummary"):
        node = clean.get(slice_name)
        if node is not None:
            out.extend(
                _extract_slice_messages(
                    node, workspace_key, f"redux.{slice_name}", is_partial=True
                )
            )

    # channelLatests previews (lowest rank, partial)
    latests = as_dict(clean.get("channelLatests"))
    for cid, raw in latests.items():
        if isinstance(raw, dict) and is_message(raw):
            obs = observation_from_message(
                raw,
                channel_id=str(cid),
                workspace_key=workspace_key,
                source="redux.channelLatests",
            )
            if obs:
                obs.is_partial = True
                out.append(obs)
        elif isinstance(raw, dict) and raw.get("ts") and raw.get("text"):
            preview = {
                "type": "message",
                "ts": raw.get("ts"),
                "text": raw.get("text"),
                "user": raw.get("user"),
                "channel": cid,
            }
            obs = observation_from_message(
                preview,
                channel_id=str(cid),
                workspace_key=workspace_key,
                source="redux.channelLatests",
            )
            if obs:
                obs.is_partial = True
                out.append(obs)
    return out


def extract_reactions(state: Mapping[str, Any]) -> list[ReactionSet]:
    clean = sanitize(dict(state))
    assert isinstance(clean, dict)
    out: list[ReactionSet] = []
    # reactions slice: often {channel: {ts: [{name, count, users}]}}
    rx = as_dict(clean.get("reactions"))
    for cid, bucket in rx.items():
        if not isinstance(bucket, dict):
            continue
        for ts, entries in bucket.items():
            items = entries if isinstance(entries, list) else [entries]
            for ent in items:
                if not isinstance(ent, dict):
                    continue
                emoji = ent.get("name") or ent.get("emoji")
                if not emoji:
                    continue
                users_raw = ent.get("users") or []
                users = [str(u) for u in users_raw] if isinstance(users_raw, list) else []
                count = int(ent["count"]) if isinstance(ent.get("count"), (int, float)) else len(users)
                out.append(
                    ReactionSet(
                        channel_id=str(cid),
                        ts=normalize_ts(ts) or str(ts),
                        emoji=str(emoji),
                        count=count,
                        users=users,
                    )
                )
    # Also pull reactions embedded on messages
    for obs_msg in _iter_message_dicts(clean.get("messages")):
        cid = str(obs_msg.get("channel") or "")
        ts = normalize_ts(obs_msg.get("ts"))
        if not cid or not ts:
            continue
        for ent in obs_msg.get("reactions") or []:
            if not isinstance(ent, dict):
                continue
            emoji = ent.get("name") or ent.get("emoji")
            if not emoji:
                continue
            users_raw = ent.get("users") or []
            users = [str(u) for u in users_raw] if isinstance(users_raw, list) else []
            count = int(ent["count"]) if isinstance(ent.get("count"), (int, float)) else len(users)
            out.append(
                ReactionSet(
                    channel_id=cid,
                    ts=ts,
                    emoji=str(emoji),
                    count=count,
                    users=users,
                )
            )
    return out


def _iter_message_dicts(node: Any, *, depth: int = 0) -> list[dict[str, Any]]:
    found: dict[tuple[str | None, str], dict[str, Any]] = {}
    walk_messages(node, None, found, depth=depth)
    return list(found.values())


def extract_saved_items(state: Mapping[str, Any], workspace_key: str) -> list[SavedItem]:
    clean = sanitize(dict(state))
    assert isinstance(clean, dict)
    out: list[SavedItem] = []
    for slice_name, item_type in (("saved", "saved"), ("stars", "star"), ("pins", "pin")):
        node = clean.get(slice_name)
        if node is None:
            continue
        items: list[tuple[str, Any]]
        if isinstance(node, dict):
            items = list(node.items())
        elif isinstance(node, list):
            items = [(str(i), x) for i, x in enumerate(node)]
        else:
            continue
        for iid, raw in items:
            if not isinstance(raw, dict):
                continue
            c = sanitize(raw)
            assert isinstance(c, dict)
            channel_id = c.get("channel") or c.get("channel_id")
            ts = c.get("ts") or c.get("message_ts")
            # nested message
            msg = c.get("message") if isinstance(c.get("message"), dict) else None
            if msg:
                channel_id = channel_id or msg.get("channel")
                ts = ts or msg.get("ts")
            out.append(
                SavedItem(
                    workspace_key=workspace_key,
                    item_type=item_type,
                    item_id=str(c.get("id") or iid),
                    channel_id=str(channel_id) if channel_id else None,
                    ts=normalize_ts(ts) if ts else None,
                    state=str(c["state"]) if c.get("state") else None,
                    date_created=float(c["date_created"])
                    if isinstance(c.get("date_created"), (int, float))
                    else None,
                    date_due=float(c["date_due"])
                    if isinstance(c.get("date_due"), (int, float))
                    else None,
                    raw=c,
                )
            )
    return out


def extract_bots_as_users(state: Mapping[str, Any], workspace_key: str) -> list[User]:
    clean = sanitize(dict(state))
    assert isinstance(clean, dict)
    out: list[User] = []
    for bid, b in as_dict(clean.get("bots")).items():
        if not isinstance(b, dict):
            continue
        out.append(
            User(
                user_id=str(b.get("id") or bid),
                team_id=str(b["team_id"]) if b.get("team_id") else None,
                name=b.get("name"),
                real_name=b.get("name"),
                display_name=b.get("name"),
                title=None,
                email=None,
                is_bot=True,
                is_deleted=bool(b.get("deleted")),
                is_restricted=False,
                is_ultra_restricted=False,
                is_external=False,
                tz=None,
                raw=sanitize(b) if isinstance(b, dict) else {},
                workspace_key=workspace_key,
            )
        )
    return out


def extract_user_groups(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    clean = sanitize(dict(state))
    assert isinstance(clean, dict)
    out: list[dict[str, Any]] = []
    for gid, g in as_dict(clean.get("userGroups")).items():
        if isinstance(g, dict):
            c = sanitize(g)
            if isinstance(c, dict):
                c.setdefault("id", str(gid))
                out.append(c)
    return out


def unknown_slices(state: Mapping[str, Any]) -> list[str]:
    return [str(k) for k in state if str(k) not in KNOWN_SLICES and not str(k).startswith("_")]


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
) -> tuple[
    Workspace,
    list[Channel],
    list[User],
    list[FileMeta],
    list[MessageObservation],
    list[ReactionSet],
    list[SavedItem],
]:
    persist_id, persist_user = parse_persist_key(persist_key) if persist_key else (None, None)
    ws = extract_workspace(state, persist_id=persist_id)
    if persist_user and not ws.self_user_id:
        ws.self_user_id = persist_user
    users = extract_users(state, ws.workspace_key)
    users.extend(extract_bots_as_users(state, ws.workspace_key))
    return (
        ws,
        extract_channels(state, ws.workspace_key),
        users,
        extract_files(state, ws.workspace_key),
        extract_messages(state, ws.workspace_key),
        extract_reactions(state),
        extract_saved_items(state, ws.workspace_key),
    )
