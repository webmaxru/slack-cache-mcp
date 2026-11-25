"""Slack-style query operator parser."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

_OP_RE = re.compile(
    r"""
    (?<!\S)(?P<op>in|from|with|before|after|on|during|has|is|workspace):
    (?P<val>"[^"]+"|\S+)
    """,
    re.VERBOSE | re.IGNORECASE,
)

# All operators in this set are parsed; values validated in unsupported_operators.
UNSUPPORTED_OPS: frozenset[str] = frozenset()

_LOCAL_TZ = ZoneInfo("Europe/Paris")


@dataclass
class ParsedQuery:
    free_text: str
    filters: dict[str, Any] = field(default_factory=dict)


def parse_query(query: str) -> ParsedQuery:
    """Parse operators out of ``query``.

    Unsupported has/is values are rejected via ``unsupported_operators()``.
    """
    filters: dict[str, Any] = {}
    parts: list[str] = []
    last = 0
    for m in _OP_RE.finditer(query):
        parts.append(query[last : m.start()])
        op = m.group("op").lower()
        val = m.group("val").strip('"')
        if op == "in":
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
        if val not in {"file", "link", "reaction"}:
            found.append(f"has:{val}")
    for val in parsed.filters.get("is", []) or []:
        if val not in {"thread", "deleted", "reply", "edited"}:
            found.append(f"is:{val}")
    return found


def parse_time_bound(value: str, *, end: bool = False) -> float:
    """Convert Slack date/datetime/unix-ts / during:YYYY-MM to Unix timestamp.

    Calendar dates are interpreted in Europe/Paris (PLAN §10.5) then stored as UTC.
    """
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass

    # during:YYYY-MM
    m = re.fullmatch(r"(\d{4})-(\d{2})", value)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        if end:
            last = calendar.monthrange(year, month)[1]
            dt = datetime(year, month, last, 23, 59, 59, tzinfo=_LOCAL_TZ) + timedelta(seconds=1)
        else:
            dt = datetime(year, month, 1, tzinfo=_LOCAL_TZ)
        return dt.timestamp()

    date_only = False
    parsed: datetime | None = None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            parsed = datetime.strptime(value, fmt).replace(tzinfo=_LOCAL_TZ)
            date_only = True
            break
        except ValueError:
            continue
    if parsed is None:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(value.rstrip("Z"), fmt).replace(tzinfo=_LOCAL_TZ)
                break
            except ValueError:
                continue
    if parsed is None:
        try:
            iso = value.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(iso)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_LOCAL_TZ)
            date_only = "T" not in value and " " not in value and len(value) <= 10
        except ValueError as exc:
            raise ValueError(f"Unrecognized date/time: {value!r}") from exc
    if date_only and end:
        parsed = parsed + timedelta(days=1)
    return parsed.timestamp()


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
