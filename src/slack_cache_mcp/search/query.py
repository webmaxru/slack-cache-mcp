"""Slack-style query operator parser."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

_OP_RE = re.compile(
    r"""
    (?<!\S)(?P<op>in|from|with|before|after|on|during|has|is|workspace):
    (?P<val>"[^"]+"|\S+)
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Operators parsed but not yet implemented end-to-end — reject rather than silently drop.
UNSUPPORTED_OPS = frozenset({"with", "during"})


@dataclass
class ParsedQuery:
    free_text: str
    filters: dict[str, Any] = field(default_factory=dict)


def parse_query(query: str) -> ParsedQuery:
    filters: dict[str, Any] = {}
    parts: list[str] = []
    last = 0
    for m in _OP_RE.finditer(query):
        parts.append(query[last : m.start()])
        op = m.group("op").lower()
        val = m.group("val").strip('"')
        if op == "in":
            # Keep @user for IM resolution (PLAN §10.5); strip channel # only.
            filters["in"] = val if val.startswith("@") else val.lstrip("#")
        elif op == "from":
            filters["from"] = val.lstrip("@")
        elif op == "with":
            filters["with"] = val.lstrip("@")
        elif op in {"before", "after", "on", "during"}:
            filters[op] = val
        elif op == "has":
            filters.setdefault("has", []).append(val.lower())
        elif op == "is":
            filters.setdefault("is", []).append(val.lower())
        elif op == "workspace":
            filters["workspace"] = val
        last = m.end()
    parts.append(query[last:])
    free = " ".join(p.strip() for p in parts if p.strip())
    return ParsedQuery(free_text=free, filters=filters)


def unsupported_operators(parsed: ParsedQuery) -> list[str]:
    found: list[str] = []
    for op in UNSUPPORTED_OPS:
        if op in parsed.filters:
            found.append(op)
    for val in parsed.filters.get("has", []) or []:
        if val not in {"file", "link"}:
            found.append(f"has:{val}")
    for val in parsed.filters.get("is", []) or []:
        if val not in {"thread", "deleted"}:
            found.append(f"is:{val}")
    return found


def parse_time_bound(value: str, *, end: bool = False) -> float:
    """Convert Slack date/datetime/unix-ts strings to a Unix timestamp float."""
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass
    date_only = False
    dt: datetime | None = None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(value, fmt).replace(tzinfo=UTC)
            date_only = True
            break
        except ValueError:
            continue
    if dt is None:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(value.rstrip("Z"), fmt).replace(tzinfo=UTC)
                break
            except ValueError:
                continue
    if dt is None:
        try:
            iso = value.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            date_only = "T" not in value and " " not in value and len(value) <= 10
        except ValueError as exc:
            raise ValueError(f"Unrecognized date/time: {value!r}") from exc
    if date_only and end:
        dt = dt + timedelta(days=1)
    return dt.timestamp()


def fts_match_from_terms(terms: str, mode: str) -> str:
    """Build an FTS5 MATCH expression."""
    terms = terms.strip()
    if not terms:
        return '""'
    if mode == "phrase":
        return f'"{terms.replace(chr(34), "")}"'
    tokens = ['"' + t.replace('"', '""') + '"' for t in re.split(r"\s+", terms) if t]
    if mode == "any":
        return " OR ".join(tokens)
    # all / auto / lexical default: AND
    return " AND ".join(tokens)
