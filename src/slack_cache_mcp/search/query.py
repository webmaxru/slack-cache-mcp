"""Slack-style query operator parser."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_OP_RE = re.compile(
    r"""
    (?P<op>in|from|with|before|after|on|during|has|is|workspace):
    (?P<val>"[^"]+"|\S+)
    """,
    re.VERBOSE | re.IGNORECASE,
)


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
            filters["in"] = val.lstrip("#@")
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


def fts_match_from_terms(terms: str, mode: str) -> str:
    """Build an FTS5 MATCH expression."""
    terms = terms.strip()
    if not terms:
        return '""'
    if mode == "phrase":
        return f'"{terms.replace(chr(34), "")}"'
    tokens = [t for t in re.split(r"\s+", terms) if t]
    if mode == "any":
        return " OR ".join(tokens)
    # all / auto / lexical default: AND
    return " AND ".join(tokens)
