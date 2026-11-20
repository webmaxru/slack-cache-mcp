"""Domain dataclasses for archive entities."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Workspace:
    workspace_key: str
    team_id: str | None
    enterprise_id: str | None
    name: str | None
    domain: str | None
    self_user_id: str | None
    self_is_guest: bool
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Channel:
    channel_id: str
    workspace_key: str
    context_team_id: str | None
    name: str | None
    kind: str
    is_ext_shared: bool
    is_archived: bool
    is_member: bool
    topic: str | None
    purpose: str | None
    im_user_id: str | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class User:
    user_id: str
    team_id: str | None
    name: str | None
    real_name: str | None
    display_name: str | None
    title: str | None
    email: str | None
    is_bot: bool
    is_deleted: bool
    is_restricted: bool
    is_ultra_restricted: bool
    is_external: bool
    tz: str | None
    raw: dict[str, Any] = field(default_factory=dict)
    workspace_key: str | None = None


@dataclass
class FileMeta:
    file_id: str
    workspace_key: str | None
    user_id: str | None
    name: str | None
    title: str | None
    mimetype: str | None
    filetype: str | None
    size: int | None
    preview_text: str | None
    raw: dict[str, Any] = field(default_factory=dict)
    local_path: str | None = None
    local_sha256: str | None = None
    ocr_text: str | None = None
    created: float | None = None


@dataclass
class ReactionSet:
    channel_id: str
    ts: str
    emoji: str
    count: int
    users: list[str] = field(default_factory=list)


@dataclass
class SavedItem:
    workspace_key: str
    item_type: str
    item_id: str
    channel_id: str | None = None
    ts: str | None = None
    state: str | None = None
    date_created: float | None = None
    date_due: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class CanvasDoc:
    canvas_id: str
    workspace_key: str | None
    channel_id: str | None
    title: str | None
    text: str
    content_sha256: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Draft:
    workspace_key: str
    draft_id: str
    channel_id: str | None
    thread_ts: str | None
    text: str
    last_updated: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class MessageObservation:
    channel_id: str
    ts: str
    workspace_key: str
    user_id: str | None
    thread_ts: str | None
    subtype: str | None
    text: str
    search_text: str
    content_sha256: str
    edited_ts: str | None
    is_deletion: bool
    is_partial: bool
    source: str
    source_rank: int
    raw: dict[str, Any] = field(default_factory=dict)
    reply_count: int | None = None
    latest_reply: str | None = None
    reply_users: list[str] = field(default_factory=list)
    file_ids: list[str] = field(default_factory=list)
