"""ArchiveRepository protocol — MCP and CLI talk only to this."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from slack_cache_mcp.domain.models import (
    Channel,
    MessageObservation,
    User,
    Workspace,
)


@runtime_checkable
class ArchiveRepository(Protocol):
    def ensure_schema(self) -> None: ...

    def upsert_device(self, device_id: str, label: str) -> None: ...

    def upsert_source(
        self,
        source_id: str,
        device_id: str,
        kind: str,
        path_hint: str,
        slack_version: str | None,
    ) -> None: ...

    def begin_snapshot(
        self,
        snapshot_id: str,
        source_id: str,
        trigger: str,
    ) -> None: ...

    def finish_snapshot(
        self,
        snapshot_id: str,
        *,
        files_copied: int,
        bytes_copied: int,
        states_decoded: int,
        decode_errors: int,
        stats_json: str,
    ) -> None: ...

    def has_state_generation(self, state_sha256: str) -> bool: ...

    def touch_state_generation(self, state_sha256: str, seen_at: str) -> None: ...

    def insert_state_generation(
        self,
        state_sha256: str,
        workspace_key: str,
        self_user_id: str | None,
        blob_ref: str | None,
        snapshot_id: str,
        seen_at: str,
    ) -> None: ...

    def upsert_workspace(self, ws: Workspace, hlc: str, seen_at: str) -> None: ...

    def upsert_channel(self, ch: Channel, hlc: str, seen_at: str) -> None: ...

    def upsert_user(self, user: User, hlc: str, seen_at: str) -> None: ...

    def ingest_observation(
        self,
        obs: MessageObservation,
        *,
        device_id: str,
        snapshot_id: str,
        state_sha256: str,
        seen_at: str,
        hlc: str,
    ) -> None: ...

    def reproject_message(self, channel_id: str, ts: str) -> None: ...

    def list_workspaces(self) -> list[dict[str, Any]]: ...

    def list_channels(
        self,
        *,
        workspace: str | None = None,
        query: str | None = None,
        kinds: list[str] | None = None,
        only_with_messages: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...

    def get_message(
        self,
        *,
        channel: str,
        ts: str,
        versions: bool = False,
    ) -> dict[str, Any] | None: ...

    def read_channel(
        self,
        channel: str,
        *,
        after: str | None = None,
        before: str | None = None,
        limit: int = 50,
        order: str = "newest",
        include_replies: bool = False,
    ) -> list[dict[str, Any]]: ...

    def get_thread(
        self,
        channel: str,
        thread_ts: str,
        *,
        limit: int = 200,
    ) -> dict[str, Any]: ...

    def get_context(
        self,
        channel: str,
        ts: str,
        *,
        before: int = 5,
        after: int = 5,
    ) -> dict[str, Any]: ...

    def resolve(
        self,
        name_or_id: str,
        *,
        kind: str = "any",
        workspace: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def get_user(self, user: str, workspace: str | None = None) -> dict[str, Any] | None: ...

    def cache_status(self, workspace: str | None = None, channel: str | None = None) -> dict[str, Any]: ...

    def search_fts(
        self,
        match_sql: str,
        *,
        limit: int,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]: ...

    def search_trigram(
        self,
        needle: str,
        *,
        limit: int,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]: ...

    def scan_for_token_leak(self) -> list[str]: ...
