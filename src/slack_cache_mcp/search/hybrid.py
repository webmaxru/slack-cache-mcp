"""Hybrid ranking: RRF fusion of lexical + vector lists."""

from __future__ import annotations

from typing import Any

from slack_cache_mcp.search.embedder import mmr_select, rrf_score


def fuse_rrf(
    lexical: list[dict[str, Any]],
    vector: list[dict[str, Any]],
    *,
    w_lex: float = 1.0,
    w_vec: float = 1.0,
    k: int = 60,
    id_keys: tuple[str, ...] = ("channel_id", "ts"),
) -> list[dict[str, Any]]:
    """Fuse two ranked lists with Reciprocal Rank Fusion.

    Vector contributions are deduplicated by message identity so message/thread/
    window docs that resolve to the same `(channel_id, ts)` only score once
    (best/first occurrence keeps the rank slot).
    """

    def _id(hit: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(hit.get(key) for key in id_keys)

    scores: dict[tuple[Any, ...], float] = {}
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    lex_rank: dict[tuple[Any, ...], int] = {}
    vec_rank: dict[tuple[Any, ...], int] = {}

    for i, hit in enumerate(lexical, start=1):
        key = _id(hit)
        scores[key] = scores.get(key, 0.0) + rrf_score(i, k=k, weight=w_lex)
        lex_rank[key] = i
        merged[key] = dict(hit)

    # Deduplicate vector list by message identity before assigning ranks.
    seen_vec: set[tuple[Any, ...]] = set()
    vec_unique: list[dict[str, Any]] = []
    for hit in vector:
        key = _id(hit)
        if key in seen_vec:
            continue
        seen_vec.add(key)
        vec_unique.append(hit)

    for i, hit in enumerate(vec_unique, start=1):
        key = _id(hit)
        scores[key] = scores.get(key, 0.0) + rrf_score(i, k=k, weight=w_vec)
        vec_rank[key] = i
        if key not in merged:
            merged[key] = dict(hit)
        else:
            base = merged[key]
            for field, value in hit.items():
                if field not in base or base[field] in (None, ""):
                    base[field] = value
            merged[key] = base

    out: list[dict[str, Any]] = []
    for key, hit in merged.items():
        item = dict(hit)
        item["score"] = scores.get(key, 0.0)
        item["match"] = {
            "lexical_rank": lex_rank.get(key),
            "vector_rank": vec_rank.get(key),
        }
        out.append(item)
    out.sort(key=lambda x: (-float(x["score"]), str(x.get("channel_id")), str(x.get("ts"))))
    return out


def collapse_threads(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the best hit per (channel_id, thread root); attach hit_count."""
    best: dict[tuple[str, str], dict[str, Any]] = {}
    counts: dict[tuple[str, str], int] = {}
    for h in hits:
        channel = str(h.get("channel_id") or "")
        root = str(h.get("thread_ts") or h.get("ts") or "")
        key = (channel, root)
        counts[key] = counts.get(key, 0) + 1
        prev = best.get(key)
        if prev is None or float(h.get("score") or 0) > float(prev.get("score") or 0):
            best[key] = dict(h)
    out: list[dict[str, Any]] = []
    for key, hit in best.items():
        hit["hit_count"] = counts[key]
        out.append(hit)
    out.sort(key=lambda x: (-float(x.get("score") or 0), str(x.get("channel_id")), str(x.get("ts"))))
    return out


def diversify_mmr(
    hits: list[dict[str, Any]],
    *,
    limit: int,
    lambda_: float = 0.7,
) -> list[dict[str, Any]]:
    selected = mmr_select(hits, lambda_=lambda_, limit=limit, diversity_key="channel_id")
    return selected  # list[dict[str, object]] is compatible for callers
