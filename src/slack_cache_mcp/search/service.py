"""Search service over ArchiveRepository (lexical + semantic hybrid)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from slack_cache_mcp.search.embedder import HashingEmbedder
from slack_cache_mcp.search.hybrid import collapse_threads, diversify_mmr, fuse_rrf
from slack_cache_mcp.search.query import (
    fts_match_from_terms,
    parse_during_bounds,
    parse_query,
    parse_time_bound,
    unsupported_operators,
)
from slack_cache_mcp.store.sqlite.repo import SqliteRepository

try:
    from slack_cache_mcp.search.embedder import Embedder
except ImportError:  # pragma: no cover
    Embedder = Any  # type: ignore[misc, assignment]


class SearchService:
    def __init__(
        self,
        repo: SqliteRepository,
        embedder: Embedder | None = None,
        *,
        models_dir: Path | None = None,
    ) -> None:
        self.repo = repo
        self.embedder: Embedder = embedder or HashingEmbedder()
        self.models_dir = models_dir

    def _sync_embedder_with_archive(self) -> None:
        """Reload reader embedder when daemon has switched the archive model."""
        try:
            row = self.repo._conn.execute(  # noqa: SLF001
                "SELECT value FROM meta WHERE key='embedding_model'"
            ).fetchone()
        except Exception:
            return
        archive_model = str(row["value"]) if row else None
        if not archive_model or archive_model == self.embedder.model_id:
            return
        try:
            from slack_cache_mcp.embeddings.factory import load_embedder

            # Load the archive's exact model — not "best available" (ONNX files
            # must not override an archive that switched to hashing).
            fresh = load_embedder(
                models_dir=self.models_dir, model_id=archive_model
            )
            # Strict id match only (legacy hashing spaces are not interchangeable).
            if fresh.model_id == archive_model:
                self.embedder = fresh
        except Exception:
            pass

    def _resolve_channel_ids(self, values: list[str]) -> list[str]:
        ids: list[str] = []
        for raw in values:
            v = raw.strip()
            if not v:
                continue
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

    def _build_filters(
        self,
        parsed: Any,
        *,
        include_deleted: bool,
        workspace: str | None,
        channels: list[str] | None,
        users: list[str] | None,
        after: str | None,
        before: str | None,
    ) -> dict[str, Any]:
        filters: dict[str, Any] = {}
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

        after_v = after or parsed.filters.get("after")
        before_v = before or parsed.filters.get("before")
        on_v = parsed.filters.get("on")
        during_v = parsed.filters.get("during")
        if on_v:
            filters["after"] = parse_time_bound(on_v, end=False)
            filters["before"] = parse_time_bound(on_v, end=True)
        elif during_v:
            start, end = parse_during_bounds(str(during_v))
            filters["after"] = start
            filters["before"] = end
        else:
            if after_v:
                filters["after"] = parse_time_bound(str(after_v), end=False)
            if before_v:
                filters["before"] = parse_time_bound(str(before_v), end=False)

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

        # with: is an independent participant filter (thread contains user), not OR with from:
        if parsed.filters.get("with"):
            filters["with_user_ids"] = self._resolve_user_ids([str(parsed.filters["with"])])

        for h in parsed.filters.get("has") or []:
            if h == "file":
                filters["has_files"] = True
            elif h == "link":
                filters["has_links"] = True
            elif h == "reaction":
                filters["has_reaction"] = True

        if "thread" in is_vals:
            filters["is_thread"] = True
        if "reply" in is_vals:
            filters["is_reply"] = True
        if "edited" in is_vals:
            filters["is_edited"] = True
        return filters

    def _coverage_note(self, *, mode: str) -> str | None:
        if mode not in {"hybrid", "semantic"}:
            return None
        self._sync_embedder_with_archive()
        status = self.repo.embedding_coverage()
        if not status.get("vec_available"):
            return "semantic index unavailable; hybrid fell back to lexical-only fusion"
        pending = int(status.get("pending") or 0)
        embedded = int(status.get("embedded") or 0)
        notes: list[str] = []
        try:
            row = self.repo._conn.execute(  # noqa: SLF001
                "SELECT value FROM meta WHERE key='embedding_model'"
            ).fetchone()
            archive_model = str(row["value"]) if row else None
            if archive_model and archive_model != self.embedder.model_id:
                notes.append(
                    f"reader embedder {self.embedder.model_id} differs from "
                    f"archive index {archive_model}"
                )
        except Exception:
            pass
        if embedded == 0:
            notes.append("no documents embedded yet; semantic leg empty")
        elif pending > 0:
            notes.append(
                f"partial semantic coverage: {embedded} embedded, {pending} pending"
            )
        return "; ".join(notes) if notes else None

    def search_messages(
        self,
        *,
        query: str,
        mode: str = "hybrid",
        limit: int = 10,
        include_deleted: bool = False,
        workspace: str | None = None,
        channels: list[str] | None = None,
        users: list[str] | None = None,
        after: str | None = None,
        before: str | None = None,
        context: str = "none",
        context_size: int = 3,
        sort: str = "relevance",
    ) -> dict[str, Any]:
        parsed = parse_query(query)
        bad = unsupported_operators(parsed)
        if bad:
            raise ValueError(
                "Unsupported search operators: "
                + ", ".join(bad)
                + " (supported: in, from, with, before, after, on, during, "
                "has:file|link|reaction, is:thread|reply|edited|deleted, workspace)"
            )

        filters = self._build_filters(
            parsed,
            include_deleted=include_deleted,
            workspace=workspace,
            channels=channels,
            users=users,
            after=after,
            before=before,
        )

        free = parsed.free_text.strip()
        candidate_limit = max(limit * 10, 100)
        order = "ASC" if sort == "oldest" else "DESC"

        if not free:
            fused = self.repo.search_filtered(
                limit=candidate_limit, filters=filters, order=order
            )
            effective_mode = "filter"
        elif mode == "exact":
            fused = self.repo.search_trigram(free, limit=candidate_limit, filters=filters)
            effective_mode = "exact"
        elif mode == "semantic":
            fused = self._semantic(free, limit=candidate_limit, filters=filters)
            effective_mode = "semantic"
        elif mode in {"lexical", "phrase", "any", "all"}:
            fused = self._lexical(free, mode=mode, limit=candidate_limit, filters=filters)
            effective_mode = mode if mode != "all" else "lexical"
        else:
            lex = self._lexical(free, mode="all", limit=candidate_limit, filters=filters)
            vec = self._semantic(free, limit=candidate_limit, filters=filters)
            fused = fuse_rrf(lex, vec)
            effective_mode = "hybrid"

        collapsed = collapse_threads(fused)

        if sort in {"newest", "oldest"}:
            reverse = sort == "newest"
            diversified = sorted(
                collapsed,
                key=lambda h: float(h.get("ts") or 0),
                reverse=reverse,
            )[:limit]
        else:
            diversified = diversify_mmr(collapsed, limit=limit)

        results: list[dict[str, Any]] = []
        for h in diversified:
            item = dict(h)
            if context == "thread" and item.get("thread_ts"):
                thr = self.repo.get_thread(
                    item["channel_id"], item["thread_ts"] or item["ts"], limit=context_size
                )
                item["context"] = thr
            elif context == "window" and item.get("channel_id") and item.get("ts"):
                item["context"] = self.repo.get_context(
                    item["channel_id"], item["ts"], before=context_size, after=context_size
                )
            results.append(item)

        return {
            "results": results,
            "mode": effective_mode,
            "coverage_note": self._coverage_note(mode=effective_mode),
            "next_cursor": None,
            "embedder": self.embedder.model_id,
        }

    def _lexical(
        self,
        free: str,
        *,
        mode: str,
        limit: int,
        filters: dict[str, Any],
    ) -> list[dict[str, Any]]:
        fts_mode = "phrase" if mode == "phrase" else "all"
        if mode == "any":
            fts_mode = "any"
        match = fts_match_from_terms(free, fts_mode)
        return self.repo.search_fts(match, limit=limit, filters=filters)

    def _semantic(
        self,
        free: str,
        *,
        limit: int,
        filters: dict[str, Any],
    ) -> list[dict[str, Any]]:
        self._sync_embedder_with_archive()
        qvec = self.embedder.embed_query(free)
        return self.repo.search_vector(
            qvec,
            limit=limit,
            filters=filters,
            kinds=["message", "thread", "window"],
            model_id=self.embedder.model_id,
        )


def re_looks_like_channel_id(v: str) -> bool:
    return len(v) >= 2 and v[0] in {"C", "G", "D"} and v[1:].isalnum()


def re_looks_like_user_id(v: str) -> bool:
    return len(v) >= 2 and v[0] == "U" and v[1:].isalnum()
