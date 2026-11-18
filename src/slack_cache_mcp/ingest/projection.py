"""Deterministic current-version projection (PLAN §8.3)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class VersionCandidate:
    channel_id: str
    ts: str
    content_sha256: str
    edited_ts: str | None
    is_partial: bool
    is_deletion: bool
    source_rank: int
    last_seen: str
    text: str
    search_text: str
    user_id: str | None
    thread_ts: str | None
    subtype: str | None
    reply_count: int | None = None
    latest_reply: str | None = None
    reply_users: tuple[str, ...] = ()


def _edit_key(v: VersionCandidate) -> str:
    return v.edited_ts or v.ts


def choose_current(versions: Sequence[VersionCandidate]) -> VersionCandidate | None:
    if not versions:
        return None
    return max(
        versions,
        key=lambda v: (
            0 if v.is_partial else 1,
            _edit_key(v),
            v.source_rank,
            v.last_seen,
            v.content_sha256,
        ),
    )


def deleted_state_for(versions: Sequence[VersionCandidate], chosen: VersionCandidate) -> str:
    # Explicit tombstones are facts (PLAN §8.2). A tombstone without edited_ts still
    # marks the message deleted even when a later-edited content version is current.
    _ = chosen  # projection still picks content separately
    for v in versions:
        if v.is_deletion:
            return "deleted"
    return "none"


def max_merge_thread_meta(
    versions: Iterable[VersionCandidate],
) -> tuple[int | None, str | None, list[str]]:
    reply_count: int | None = None
    latest_reply: str | None = None
    users: set[str] = set()
    for v in versions:
        if v.reply_count is not None:
            reply_count = max(reply_count or 0, v.reply_count)
        if v.latest_reply is not None:
            if latest_reply is None or v.latest_reply > latest_reply:
                latest_reply = v.latest_reply
        users.update(v.reply_users)
    return reply_count, latest_reply, sorted(users)
