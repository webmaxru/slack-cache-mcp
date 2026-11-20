"""Canvas IndexedDB decoder: rebuild document text from syncer.Document / Section records."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from slack_cache_mcp.desktop.sanitize import sanitize
from slack_cache_mcp.domain.models import CanvasDoc

_CANVAS_DB_RE = re.compile(r"^canvas-(?P<user>U[A-Z0-9]+)-(?P<team>[TE][A-Z0-9]+)$")


def parse_canvas_db_name(name: str) -> tuple[str | None, str | None]:
    m = _CANVAS_DB_RE.match(name)
    if not m:
        return None, None
    return m.group("user"), m.group("team")


def _section_text(section: Mapping[str, Any]) -> str:
    for key in ("text", "content", "markdown", "plain_text", "value"):
        v = section.get(key)
        if isinstance(v, str) and v.strip():
            return v
    # Nested Quill/ProseMirror-ish
    nodes = section.get("nodes") or section.get("content_nodes")
    if isinstance(nodes, list):
        parts: list[str] = []
        for n in nodes:
            if isinstance(n, dict):
                t = n.get("text") or n.get("content")
                if isinstance(t, str):
                    parts.append(t)
        if parts:
            return "\n".join(parts)
    return ""


def rebuild_canvases(
    records: Iterable[Mapping[str, Any]],
    *,
    workspace_key: str | None = None,
) -> list[CanvasDoc]:
    """Rebuild canvases from syncer.* records (Document / Section / Index)."""
    docs: dict[str, dict[str, Any]] = {}
    sections: dict[str, dict[str, Any]] = {}
    order: dict[str, list[str]] = {}

    for raw in records:
        if not isinstance(raw, Mapping):
            continue
        key = str(raw.get("key") or raw.get("id") or "")
        val = raw.get("value") if "value" in raw else raw
        if not isinstance(val, dict):
            continue
        clean = sanitize(val)
        assert isinstance(clean, dict)

        if key.startswith("syncer.Document/") or clean.get("_type") == "Document":
            cid = key.split("/", 1)[-1] if "/" in key else str(clean.get("id") or "")
            if not cid:
                continue
            docs[cid] = clean
            continue
        if key.startswith("syncer.Section/") or clean.get("_type") == "Section":
            sid = key.split("/", 1)[-1] if "/" in key else str(clean.get("id") or "")
            if not sid:
                continue
            sections[sid] = clean
            continue
        if "Index/Section" in key or clean.get("_type") == "SectionIndex":
            doc_id = str(clean.get("document_id") or clean.get("doc_id") or "")
            ids = clean.get("section_ids") or clean.get("sections") or clean.get("order")
            if doc_id and isinstance(ids, list):
                order[doc_id] = [str(x) for x in ids]
            continue

    out: list[CanvasDoc] = []
    for cid, meta in docs.items():
        sec_ids = order.get(cid) or [
            sid
            for sid, s in sections.items()
            if str(s.get("document_id") or s.get("doc_id") or "") == cid
        ]
        # Fallback: all sections if no index
        if not sec_ids:
            sec_ids = sorted(sections.keys())
        texts: list[str] = []
        for sid in sec_ids:
            s = sections.get(sid)
            if s:
                t = _section_text(s)
                if t:
                    texts.append(t)
        body = "\n\n".join(texts)
        title = meta.get("title") or meta.get("name")
        channel_id = None
        for ck in ("channel_id", "channelId", "file_id"):
            if meta.get(ck):
                channel_id = str(meta[ck])
                break
        payload = {"document": meta, "section_ids": sec_ids, "text": body}
        blob = json.dumps(sanitize(payload), sort_keys=True, separators=(",", ":"))
        sha = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        out.append(
            CanvasDoc(
                canvas_id=cid,
                workspace_key=workspace_key,
                channel_id=channel_id,
                title=str(title) if title else None,
                text=body,
                content_sha256=sha,
                raw=sanitize(payload) if isinstance(sanitize(payload), dict) else {},
            )
        )
    return out


def extract_from_idb_records(
    records: Iterable[Any],
    *,
    database: str | None = None,
) -> list[CanvasDoc]:
    user, team = parse_canvas_db_name(database or "")
    workspace_key = team
    rows: list[dict[str, Any]] = []
    for rec in records:
        if hasattr(rec, "key") and hasattr(rec, "value"):
            rows.append({"key": rec.key, "value": rec.value})
        elif isinstance(rec, dict):
            rows.append(rec)
    return rebuild_canvases(rows, workspace_key=workspace_key)
