"""Coverage ranges and suspected-deletion detection (PLAN §8.2)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ChannelWindow:
    channel_id: str
    start_ts: str
    end_ts: str
    contiguous_to_latest: bool
    message_ts: frozenset[str]


def _ts_key(ts: str) -> float:
    try:
        return float(ts)
    except ValueError:
        return 0.0


def contiguous_ranges(timestamps: list[str]) -> list[tuple[str, str]]:
    """Merge sorted ts into contiguous ranges (adjacency by sorted order = one range).

    For Slack cache windows we treat the set of observed ts in one state as one
    contiguous window from min to max (PLAN: window min/max with no has_more gap).
    """
    if not timestamps:
        return []
    ordered = sorted(timestamps, key=_ts_key)
    return [(ordered[0], ordered[-1])]


def window_from_channel_messages(
    channel_id: str,
    timestamps: list[str],
    *,
    has_more_older: bool = False,
    contiguous_to_latest: bool = True,
) -> ChannelWindow | None:
    if not timestamps:
        return None
    ranges = contiguous_ranges(timestamps)
    start, end = ranges[0]
    return ChannelWindow(
        channel_id=channel_id,
        start_ts=start,
        end_ts=end,
        contiguous_to_latest=contiguous_to_latest and not has_more_older,
        message_ts=frozenset(timestamps),
    )


def extract_channel_windows(state: dict[str, Any]) -> list[ChannelWindow]:
    """Build per-channel windows from a sanitized Redux ``messages`` slice.

    Uses ``channelHistory`` / ``channelCursors`` when present for has_more hints;
    otherwise assumes the observed ts set is the contiguous window.
    """
    messages = state.get("messages")
    if not isinstance(messages, dict):
        return []
    history = state.get("channelHistory") if isinstance(state.get("channelHistory"), dict) else {}
    cursors = state.get("channelCursors") if isinstance(state.get("channelCursors"), dict) else {}
    out: list[ChannelWindow] = []
    for channel_id, bucket in messages.items():
        if not isinstance(channel_id, str) or not isinstance(bucket, dict):
            continue
        # normalize keys that look like ts
        normalized: list[str] = []
        for t in bucket:
            if isinstance(t, (int, float)):
                normalized.append(f"{float(t):.6f}")
            elif isinstance(t, str) and t:
                normalized.append(t)
        has_more = False
        hist = history.get(channel_id) if isinstance(history, dict) else None
        if isinstance(hist, dict):
            has_more = bool(hist.get("has_more") or hist.get("hasMore"))
        cur = cursors.get(channel_id) if isinstance(cursors, dict) else None
        if isinstance(cur, dict) and cur.get("has_more_older") is not None:
            has_more = bool(cur.get("has_more_older"))
        win = window_from_channel_messages(
            channel_id,
            normalized,
            has_more_older=has_more,
            contiguous_to_latest=not has_more,
        )
        if win is not None:
            out.append(win)
    return out


def merge_coverage_interval(
    existing: list[tuple[str, str]],
    start_ts: str,
    end_ts: str,
) -> list[tuple[str, str]]:
    """Union intervals for a channel (lexicographic/numeric by float ts)."""
    intervals = [*existing, (start_ts, end_ts)]
    intervals.sort(key=lambda p: (_ts_key(p[0]), _ts_key(p[1])))
    merged: list[tuple[str, str]] = []
    for s, e in intervals:
        if not merged:
            merged.append((s, e))
            continue
        ps, pe = merged[-1]
        if _ts_key(s) <= _ts_key(pe):
            # overlap or touch
            merged[-1] = (ps, e if _ts_key(e) > _ts_key(pe) else pe)
        else:
            merged.append((s, e))
    return merged


def ts_in_window(ts: str, start_ts: str, end_ts: str) -> bool:
    return _ts_key(start_ts) <= _ts_key(ts) <= _ts_key(end_ts)


def suspected_missing(
    *,
    previously_seen: set[str],
    window: ChannelWindow,
) -> set[str]:
    """Return previously-seen ts that fall inside this contiguous window but are absent.

    Eviction outside the window is ignored. Caller tracks consecutive-generation count.
    """
    if not window.contiguous_to_latest and not window.message_ts:
        return set()
    # Only flag when the window claims to be contiguous (no has_more gap).
    # PLAN: contiguous and covers ts yet message absent.
    missing: set[str] = set()
    for ts in previously_seen:
        if ts in window.message_ts:
            continue
        if ts_in_window(ts, window.start_ts, window.end_ts):
            missing.add(ts)
    return missing
