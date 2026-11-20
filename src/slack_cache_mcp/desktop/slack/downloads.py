"""Root-state downloads + opt-in local file text extraction (never fetch)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from slack_cache_mcp.desktop.sanitize import sanitize
from slack_cache_mcp.domain.models import FileMeta


def load_downloads(mirror: Path) -> list[dict[str, Any]]:
    from slack_cache_mcp.desktop.slack import rootstate

    return rootstate.load_downloads(mirror)


def _extract_text(path: Path, *, max_bytes: int = 2_000_000) -> str | None:
    suffix = path.suffix.lower()
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > max_bytes:
        return None
    if suffix in {".txt", ".md", ".csv", ".json", ".log"}:
        try:
            return path.read_text(encoding="utf-8", errors="replace")[:100_000]
        except OSError:
            return None
    # pdf/docx: best-effort without heavy deps — skip if not plain text
    return None


def index_local_downloads(
    downloads: list[dict[str, Any]],
    *,
    workspace_key: str | None = None,
    extract_text: bool = True,
) -> list[FileMeta]:
    """Index local download paths referenced by root-state (opt-in)."""
    out: list[FileMeta] = []
    for row in downloads:
        clean = sanitize(row) if isinstance(row, dict) else {}
        assert isinstance(clean, dict)
        path_s = clean.get("downloadPath") or clean.get("path") or clean.get("local_path")
        name = clean.get("fileName") or clean.get("name")
        fid = str(clean.get("id") or clean.get("file_id") or name or "download")
        local_sha = None
        preview = None
        size = clean.get("size")
        size_i = int(size) if isinstance(size, (int, float)) else None
        local_path = None
        if isinstance(path_s, str) and path_s:
            p = Path(path_s)
            # Only read if the path exists AND is not under a Slack profile credential area.
            # Never follow into Network/Cookies.
            denied = {"Network", "Cookies", "Local State", "Login Data"}
            if any(part in denied for part in p.parts):
                continue
            if p.is_file():
                local_path = str(p)
                try:
                    data = p.read_bytes()
                    local_sha = hashlib.sha256(data).hexdigest()
                    size_i = len(data)
                    if extract_text:
                        preview = _extract_text(p)
                except OSError:
                    pass
            else:
                # Fixture may supply preview_text without a real file
                local_path = path_s
        if isinstance(clean.get("preview_text"), str):
            preview = preview or clean["preview_text"]
        out.append(
            FileMeta(
                file_id=fid if fid.startswith("F") else f"FDL_{hashlib.sha256(fid.encode()).hexdigest()[:12]}",
                workspace_key=workspace_key,
                user_id=str(clean["user"]) if clean.get("user") else None,
                name=str(name) if name else (Path(path_s).name if path_s else None),
                title=str(clean["title"]) if clean.get("title") else None,
                mimetype=str(clean["mimetype"]) if clean.get("mimetype") else None,
                filetype=str(clean["filetype"]) if clean.get("filetype") else None,
                size=size_i,
                preview_text=preview,
                raw=clean,
                local_path=local_path,
                local_sha256=local_sha,
            )
        )
    return out
