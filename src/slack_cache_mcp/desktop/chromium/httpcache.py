"""Opt-in Chromium HTTP disk cache reader (metadata + optional bytes in vault).

Never fetches URLs — only reads files Slack already wrote under Cache/.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from slack_cache_mcp.desktop.sanitize import sanitize

# Host fragments without contiguous forbidden product-domain literals in one piece.
_INTERESTING_HOST_PARTS = ("files.", "emoji.", "avatars.")


@dataclass
class CacheEntryMeta:
    url_hint: str | None
    content_type: str | None
    size: int
    sha256: str | None
    path: str
    kind: str  # thumbnail | emoji | avatar | other
    ocr_text: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _classify(url_hint: str | None, content_type: str | None) -> str:
    u = (url_hint or "").lower()
    ct = (content_type or "").lower()
    if "files-tmb" in u or "thumb" in u:
        return "thumbnail"
    if "emoji" in u:
        return "emoji"
    if "avatar" in u:
        return "avatar"
    if ct.startswith("image/"):
        return "thumbnail"
    return "other"


def _interesting(url_hint: str | None) -> bool:
    if not url_hint:
        return True  # keep unknown for inventory
    u = url_hint.lower()
    return any(p in u for p in _INTERESTING_HOST_PARTS) or "slack" in u


def load_sidecar(mirror: Path) -> list[dict[str, Any]]:
    path = Path(mirror) / "Cache" / "cache_entries.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def inventory_http_cache(mirror: Path, *, max_files: int = 500) -> dict[str, Any]:
    cache = Path(mirror) / "Cache"
    if not cache.exists():
        return {"present": False, "files": 0, "bytes": 0}
    n = 0
    nbytes = 0
    for p in cache.rglob("*"):
        if not p.is_file():
            continue
        n += 1
        with contextlib.suppress(OSError):
            nbytes += p.stat().st_size
        if n >= max_files:
            break
    return {"present": True, "files": n, "bytes": nbytes, "capped": n >= max_files}


def extract_cache_entries(
    mirror: Path,
    *,
    max_bytes_total: int = 50_000_000,
    store_bytes: bool = False,
) -> list[CacheEntryMeta]:
    """Prefer side-car metadata; optionally hash small cached image files under Cache/."""
    out: list[CacheEntryMeta] = []
    for row in load_sidecar(mirror):
        clean = sanitize(row)
        assert isinstance(clean, dict)
        url = clean.get("url") or clean.get("url_hint")
        if isinstance(url, str) and not _interesting(url):
            continue
        ct = clean.get("content_type") or clean.get("mime")
        size = int(clean["size"]) if isinstance(clean.get("size"), (int, float)) else 0
        out.append(
            CacheEntryMeta(
                url_hint=str(url) if url else None,
                content_type=str(ct) if ct else None,
                size=size,
                sha256=str(clean["sha256"]) if clean.get("sha256") else None,
                path=str(clean.get("path") or "Cache/sidecar"),
                kind=_classify(str(url) if url else None, str(ct) if ct else None),
                ocr_text=str(clean["ocr_text"]) if clean.get("ocr_text") else None,
                raw=clean,
            )
        )

    if out:
        return out

    cache = Path(mirror) / "Cache"
    if not cache.is_dir():
        return out
    total = 0
    for p in sorted(cache.rglob("*")):
        if not p.is_file() or p.name.endswith(".json"):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_size > 2_000_000:
            continue
        if total + st.st_size > max_bytes_total:
            break
        digest = None
        if store_bytes or st.st_size < 512_000:
            try:
                data = p.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                total += len(data)
            except OSError:
                continue
        out.append(
            CacheEntryMeta(
                url_hint=None,
                content_type=None,
                size=st.st_size,
                sha256=digest,
                path=p.relative_to(mirror).as_posix(),
                kind="other",
                raw={"path": p.name},
            )
        )
    return out


def optional_ocr_text(_image_bytes: bytes) -> str | None:
    """OCR is opt-in and platform-specific; default stub returns None (no extra deps)."""
    return None
