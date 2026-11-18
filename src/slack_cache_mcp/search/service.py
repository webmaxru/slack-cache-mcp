"""Search service over ArchiveRepository."""

from __future__ import annotations

from typing import Any

from slack_cache_mcp.search.query import (
    fts_match_from_terms,
    parse_query,
    parse_time_bound,
    unsupported_operators,
)
from slack_cache_mcp.store.sqlite.repo import SqliteRepository


class SearchService:
    def __init__(self, repo: SqliteRepository) -> None:
        self.repo = repo

    def _resolve_channel_ids(self, values: list[str]) -> list[str]:
        ids: list[str] = []
        for raw in values:
            v = raw.strip()
            if not v:
                continue
            # Bare channel/user IDs pass through; names and @user go through resolve.
            if re_looks_like_channel_id(v):
                ids.append(v)
                continue
            resolved = self.repo.resolve(v, kind="channel" if not v.startswith("@") else "im")
            channel_hits = [r for r in resolved if r["kind"] == "channel"]
            if not channel_hits:
                raise ValueError(f"Unknown channel: {raw}")
            ids.append(channel_hits[0]["id"])
        return ids

    def _resolve_user_ids(self, values: list[str]) -> list[str]:
        ids: list[str] = []
        for raw in values:
            v = raw.strip().lstrip("@")
            if not v:
                continue
            if re_looks_like_user_id(v):
                ids.append(v)
                continue
            resolved = self.repo.resolve(v, kind="user")
            user_hits = [r for r in resolved if r["kind"] == "user"]
            if not user_hits:
                raise ValueError(f"Unknown user: {raw}")
            ids.append(user_hits[0]["id"])
        return ids

    def search_messages(
        self,
        *,
        query: str,
        mode: str = "lexical",
        limit: int = 10,
        include_deleted: bool = False,
        workspace: str | None = None,
        channels: list[str] | None = None,
        users: list[str] | None = None,
        after: str | None = None,
        before: str | None = None,
        context: str = "none",
        context_size: int = 3,
    ) -> dict[str, Any]:
        parsed = parse_query(query)
        bad = unsupported_operators(parsed)
        if bad:
            raise ValueError(
                "Unsupported search operators: "
                + ", ".join(bad)
                + " (supported: in, from, before, after, on, has:file|link, is:thread|deleted, workspace)"
            )

        filters: dict[str, Any] = {}
        notes: list[str] = []

        # Deletion policy
        is_vals = [str(x).lower() for x in (parsed.filters.get("is") or [])]
        if "deleted" in is_vals:
            filters["deleted_mode"] = "only"
            include_deleted = True
        elif include_deleted:
            filters["deleted_mode"] = "include"
        else:
            filters["deleted_mode"] = "exclude"
        filters["include_deleted"] = include_deleted

        ws = workspace or parsed.filters.get("workspace")
        if ws:
            filters["workspace_key"] = ws

        # Time bounds (convert calendar dates to unix ts)
        after_v = after or parsed.filters.get("after")
        before_v = before or parsed.filters.get("before")
        on_v = parsed.filters.get("on")
        if on_v:
            filters["after"] = parse_time_bound(on_v, end=False)
            filters["before"] = parse_time_bound(on_v, end=True)
        else:
            if after_v:
                filters["after"] = parse_time_bound(str(after_v), end=False)
            if before_v:
                filters["before"] = parse_time_bound(str(before_v), end=False)

        # Channel / user filters — resolve every entry; never silently widen.
        channel_vals: list[str] = list(channels or [])
        if parsed.filters.get("in"):
            channel_vals.append(str(parsed.filters["in"]))
        if channel_vals:
            filters["channel_ids"] = self._resolve_channel_ids(channel_vals)

        user_vals: list[str] = list(users or [])
        if parsed.filters.get("from"):
            user_vals.append(str(parsed.filters["from"]))
        if user_vals:
            filters["user_ids"] = self._resolve_user_ids(user_vals)

        for h in parsed.filters.get("has") or []:
            if h == "file":
                filters["has_files"] = True
            elif h == "link":
                filters["has_links"] = True

        if "thread" in is_vals:
            filters["is_thread"] = True

        note = None
        effective_mode = mode
        if mode == "hybrid":
            effective_mode = "lexical"
            notes.append("hybrid mode returns lexical results until embeddings (M4)")

        free = parsed.free_text.strip()
        if not free:
            # Operator-only / filter-only query — do not feed operators into FTS.
            hits = self.repo.search_filtered(limit=limit, filters=filters)
        elif effective_mode == "exact":
            hits = self.repo.search_trigram(free, limit=limit, filters=filters)
        else:
            fts_mode = "phrase" if effective_mode == "phrase" else "all"
            if effective_mode == "any":
                fts_mode = "any"
            match = fts_match_from_terms(free, fts_mode)
            hits = self.repo.search_fts(match, limit=limit, filters=filters)

        # Thread collapse: keep best hit per (channel, thread root)
        collapsed: list[dict[str, Any]] = []
        seen_threads: set[tuple[str, str]] = set()
        for h in hits:
            root = h.get("thread_ts") or h["ts"]
            key = (h["channel_id"], root)
            if key in seen_threads:
                continue
            seen_threads.add(key)
            if context == "thread" and h.get("thread_ts"):
                thr = self.repo.get_thread(
                    h["channel_id"], h["thread_ts"] or h["ts"], limit=context_size
                )
                h = {**h, "context": thr}
            elif context == "window":
                h = {
                    **h,
                    "context": self.repo.get_context(
                        h["channel_id"], h["ts"], before=context_size, after=context_size
                    ),
                }
            collapsed.append(h)

        if notes:
            note = "; ".join(notes)
        return {
            "results": collapsed,
            "mode": effective_mode if free else "filter",
            "coverage_note": note,
            "next_cursor": None,
        }


def re_looks_like_channel_id(v: str) -> bool:
    return len(v) >= 2 and v[0] in {"C", "G", "D"} and v[1:].isalnum()


def re_looks_like_user_id(v: str) -> bool:
    return len(v) >= 2 and v[0] == "U" and v[1:].isalnum()
