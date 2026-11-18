"""Search service over ArchiveRepository."""

from __future__ import annotations

from typing import Any

from slack_cache_mcp.search.query import fts_match_from_terms, parse_query
from slack_cache_mcp.store.sqlite.repo import SqliteRepository


class SearchService:
    def __init__(self, repo: SqliteRepository) -> None:
        self.repo = repo

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
        filters: dict[str, Any] = {"include_deleted": include_deleted}
        if workspace or parsed.filters.get("workspace"):
            filters["workspace_key"] = workspace or parsed.filters.get("workspace")
        if after or parsed.filters.get("after"):
            filters["after"] = after or parsed.filters.get("after")
        if before or parsed.filters.get("before"):
            filters["before"] = before or parsed.filters.get("before")
        if parsed.filters.get("on"):
            from datetime import UTC, datetime, timedelta

            start = datetime.fromisoformat(parsed.filters["on"]).replace(tzinfo=UTC)
            filters["after"] = str(start.timestamp())
            filters["before"] = str((start + timedelta(days=1)).timestamp())
        if channels:
            filters["channel_id"] = channels[0]
        elif parsed.filters.get("in"):
            resolved = self.repo.resolve(parsed.filters["in"], kind="channel")
            if not resolved:
                raise ValueError(f"Unknown channel: {parsed.filters['in']}")
            filters["channel_id"] = resolved[0]["id"]
        if users:
            filters["user_id"] = users[0]
        elif parsed.filters.get("from"):
            resolved = self.repo.resolve(parsed.filters["from"], kind="user")
            if resolved:
                filters["user_id"] = resolved[0]["id"]

        note = None
        effective_mode = mode
        if mode == "hybrid":
            effective_mode = "lexical"
            note = "hybrid mode returns lexical results until embeddings (M4)"

        free = parsed.free_text or query
        if effective_mode == "exact":
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
                thr = self.repo.get_thread(h["channel_id"], h["thread_ts"] or h["ts"], limit=context_size)
                h = {**h, "context": thr}
            elif context == "window":
                h = {
                    **h,
                    "context": self.repo.get_context(
                        h["channel_id"], h["ts"], before=context_size, after=context_size
                    ),
                }
            collapsed.append(h)

        return {
            "results": collapsed,
            "mode": effective_mode,
            "coverage_note": note,
            "next_cursor": None,
        }
