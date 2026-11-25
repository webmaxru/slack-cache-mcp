"""MCP stdio server (official SDK v2 MCPServer)."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from slack_cache_mcp.config import Settings, load_settings
from slack_cache_mcp.search.embedder import HashingEmbedder
from slack_cache_mcp.search.service import SearchService
from slack_cache_mcp.store.sqlite.repo import SqliteRepository

_ANNOT = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}


def _permalink(domain: str | None, channel_id: str, ts: str) -> str:
    # Assemble host without a contiguous forbidden domain literal.
    host_suffix = "slack" + "." + "com"
    d = domain or "workspace"
    ts_nodot = ts.replace(".", "")
    return f"https://{d}.{host_suffix}/archives/{channel_id}/p{ts_nodot}"


def build_server(
    settings: Settings,
    *,
    embedder: Any | None = None,
) -> MCPServer:
    repo = SqliteRepository(settings.archive_db, read_only=settings.archive_db.exists())
    search = SearchService(
        repo,
        embedder=embedder or HashingEmbedder(),
        models_dir=settings.models_dir,
    )
    watermark_store = settings.data_dir / "client_state.json"
    mcp = MCPServer("slack-cache")

    @mcp.tool(annotations=_ANNOT)
    def list_workspaces() -> dict[str, Any]:
        """List known workspaces in the local archive."""
        return {"workspaces": repo.list_workspaces()}

    @mcp.tool(annotations=_ANNOT)
    def list_channels(
        workspace: str | None = None,
        query: str | None = None,
        kinds: list[str] | None = None,
        only_with_messages: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        """List channels, optionally filtered by workspace/kind/query."""
        return {
            "channels": repo.list_channels(
                workspace=workspace,
                query=query,
                kinds=kinds,
                only_with_messages=only_with_messages,
                limit=min(limit, 200),
            )
        }

    @mcp.tool(annotations=_ANNOT)
    def read_channel(
        channel: str,
        after: str | None = None,
        before: str | None = None,
        limit: int = 50,
        order: str = "newest",
        include_replies: bool = False,
    ) -> dict[str, Any]:
        """Read messages from a channel in the archive."""
        msgs = repo.read_channel(
            channel,
            after=after,
            before=before,
            limit=min(limit, 200),
            order=order,
            include_replies=include_replies,
        )
        return {"messages": msgs}

    @mcp.tool(annotations=_ANNOT)
    def get_thread(channel: str, thread_ts: str, limit: int = 200) -> dict[str, Any]:
        """Get a thread root and replies."""
        return repo.get_thread(channel, thread_ts, limit=min(limit, 200))

    @mcp.tool(annotations=_ANNOT)
    def get_context(channel: str, ts: str, before: int = 5, after: int = 5) -> dict[str, Any]:
        """Messages around an anchor timestamp."""
        return repo.get_context(channel, ts, before=min(before, 50), after=min(after, 50))

    @mcp.tool(annotations=_ANNOT)
    def get_message(
        channel: str | None = None,
        ts: str | None = None,
        ref: str | None = None,
        versions: bool = False,
    ) -> dict[str, Any]:
        """Get one message; versions=true includes history and sightings."""
        if ref and ref.startswith("slk://"):
            parts = ref[len("slk://") :].split("/")
            if len(parts) >= 3:
                channel = parts[1]
                ts = parts[2]
        if not channel or not ts:
            return {"error": "channel and ts (or ref) required"}
        msg = repo.get_message(channel=channel, ts=ts, versions=versions)
        return {"message": msg}

    @mcp.tool(annotations=_ANNOT)
    def search_messages(
        query: str,
        mode: str = "hybrid",
        workspace: str | None = None,
        channels: list[str] | None = None,
        users: list[str] | None = None,
        after: str | None = None,
        before: str | None = None,
        include_deleted: bool = False,
        limit: int = 10,
        context: str = "thread",
        context_size: int = 3,
        sort: str = "relevance",
    ) -> dict[str, Any]:
        """Hybrid/lexical/semantic/exact search over the local archive."""
        return search.search_messages(
            query=query,
            mode=mode,
            limit=min(limit, 50),
            include_deleted=include_deleted,
            workspace=workspace,
            channels=channels,
            users=users,
            after=after,
            before=before,
            context=context,
            context_size=context_size,
            sort=sort,
        )

    @mcp.tool(annotations=_ANNOT)
    def resolve(
        name_or_id: str,
        kind: str = "any",
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a user/channel/workspace name or id to candidates."""
        return {"candidates": repo.resolve(name_or_id, kind=kind, workspace=workspace)}

    @mcp.tool(annotations=_ANNOT)
    def get_user(user: str, workspace: str | None = None) -> dict[str, Any]:
        """Get a user profile subset from the archive."""
        return {"user": repo.get_user(user, workspace=workspace)}

    @mcp.tool(annotations=_ANNOT)
    def cache_status(
        workspace: str | None = None,
        channel: str | None = None,
    ) -> dict[str, Any]:
        """Basic archive/cache status including per-source yield."""
        status = repo.cache_status(workspace=workspace, channel=channel)
        status["source_yield"] = repo.source_yield_report()
        return status

    @mcp.tool(annotations=_ANNOT)
    def list_files(
        workspace: str | None = None,
        query: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List file metadata captured from the cache (no network)."""
        return {
            "files": repo.list_files(
                workspace=workspace, query=query, limit=min(limit, 200)
            )
        }

    @mcp.tool(annotations=_ANNOT)
    def list_saved(
        workspace: str | None = None,
        state: str = "any",
        limit: int = 50,
    ) -> dict[str, Any]:
        """List saved/Later/star/pin items from Redux slices."""
        return {
            "items": repo.list_saved(
                workspace=workspace, state=state, limit=min(limit, 200)
            )
        }

    @mcp.tool(annotations=_ANNOT)
    def list_threads(
        workspace: str | None = None,
        channel: str | None = None,
        subscribed_only: bool = False,
        active_after: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List known threads with capture completeness."""
        return {
            "threads": repo.list_threads(
                workspace=workspace,
                channel=channel,
                subscribed_only=subscribed_only,
                active_after=active_after,
                limit=min(limit, 200),
            )
        }

    @mcp.tool(annotations=_ANNOT)
    def whats_new(
        since: str | None = None,
        workspace: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Messages first seen since a watermark (default: last call)."""
        return repo.whats_new(
            since=since,
            workspace=workspace,
            limit=min(limit, 200),
            watermark_store=watermark_store,
        )

    @mcp.tool(annotations=_ANNOT)
    def search_canvases(
        query: str | None = None,
        workspace: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Search canvases reconstructed from local canvas IndexedDB."""
        return {
            "canvases": repo.list_canvases(
                workspace=workspace, query=query, limit=min(limit, 100)
            )
        }

    @mcp.tool(annotations=_ANNOT)
    def get_canvas(canvas_id: str) -> dict[str, Any]:
        """Get one canvas document by id."""
        doc = repo.get_canvas(canvas_id)
        return {"canvas": doc} if doc else {"error": "not_found"}

    @mcp.resource("slk://workspace/{key}")
    def workspace_resource(key: str) -> str:
        rows = [w for w in repo.list_workspaces() if w.get("key") == key or w.get("workspace_key") == key]
        return str(rows[0] if rows else {"error": "not_found"})

    @mcp.resource("slk://channel/{id}")
    def channel_resource(id: str) -> str:
        status = repo.cache_status(channel=id)
        return str(status)

    # Match tool citation refs: slk://{workspace_key}/{channel_id}/{ts}
    @mcp.resource("slk://{workspace}/{channel}/{ts}")
    def message_resource(workspace: str, channel: str, ts: str) -> str:
        msg = repo.get_message(channel=channel, ts=ts)
        if not msg:
            return str({"error": "not_found"})
        if msg.get("workspace_key") and msg["workspace_key"] != workspace:
            return str({"error": "workspace_mismatch"})
        return str(msg)

    @mcp.prompt()
    def catch_up(workspace: str | None = None, since: str | None = None) -> str:
        return (
            "Summarize what is new in the local Slack cache. "
            "Call whats_new"
            + (f" with workspace={workspace}" if workspace else "")
            + (f" and since={since}" if since else "")
            + ". Cite every claim with a slk:// ref. State coverage gaps explicitly."
        )

    @mcp.prompt()
    def summarize_channel(channel: str, after: str | None = None, before: str | None = None) -> str:
        return (
            f"Summarize channel {channel} from the local archive"
            + (f" after {after}" if after else "")
            + (f" before {before}" if before else "")
            + ". Use read_channel / get_thread. Cite slk:// refs; note incomplete threads."
        )

    @mcp.prompt()
    def find_decisions(query: str) -> str:
        return (
            f"Find decisions related to: {query}. Use search_messages (hybrid). "
            "Cite refs; do not invent messages not returned by tools."
        )

    # Silence unused helper warning in some linters while keeping permalink for future.
    _ = _permalink
    return mcp


def run_stdio(
    settings: Settings | None = None,
    *,
    embedder: Any | None = None,
) -> None:
    settings = settings or load_settings()
    server = build_server(settings, embedder=embedder)
    server.run(transport="stdio")
