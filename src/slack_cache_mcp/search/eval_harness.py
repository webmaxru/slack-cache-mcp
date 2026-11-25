"""Synthetic search quality eval (nDCG / Recall / MRR). Offline only."""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Any

from slack_cache_mcp.domain.models import Channel, MessageObservation, User, Workspace
from slack_cache_mcp.ingest.hashing import content_sha256
from slack_cache_mcp.ingest.hlc import HLC
from slack_cache_mcp.search.docs import SearchDocsBuilder
from slack_cache_mcp.search.embedder import HashingEmbedder
from slack_cache_mcp.search.service import SearchService
from slack_cache_mcp.search.worker import embed_pending
from slack_cache_mcp.store.sqlite.repo import SqliteRepository


@dataclass
class EvalQuery:
    query: str
    relevant: list[tuple[str, str]]  # (channel_id, ts)


def dcg_at_k(gains: list[float], k: int) -> float:
    total = 0.0
    for i, g in enumerate(gains[:k], start=1):
        total += g / math.log2(i + 1)
    return total


def ndcg_at_k(ranked_ids: list[tuple[str, str]], relevant: list[tuple[str, str]], k: int) -> float:
    rel_set = {r: float(len(relevant) - i) for i, r in enumerate(relevant)}
    gains = [rel_set.get(x, 0.0) for x in ranked_ids[:k]]
    ideal = sorted(rel_set.values(), reverse=True)
    idcg = dcg_at_k(ideal, k)
    if idcg <= 0:
        return 0.0
    return dcg_at_k(gains, k) / idcg


def recall_at_k(ranked_ids: list[tuple[str, str]], relevant: list[tuple[str, str]], k: int) -> float:
    if not relevant:
        return 0.0
    hit = sum(1 for r in relevant if r in set(ranked_ids[:k]))
    return hit / len(relevant)


def mrr(ranked_ids: list[tuple[str, str]], relevant: list[tuple[str, str]]) -> float:
    rel = set(relevant)
    for i, x in enumerate(ranked_ids, start=1):
        if x in rel:
            return 1.0 / i
    return 0.0


EVAL_SYNONYMS: list[set[str]] = [
    {"quarterly budget", "finance allocation", "q3 spending plan"},
    {"deploy rollback", "revert release", "undo production push"},
    {"customer escalation", "angry client ticket", "vip support incident"},
    {"vacation policy", "pto guidelines", "leave of absence rules"},
]


def _obs(channel_id: str, workspace_key: str, ts: str, user: str, text: str) -> MessageObservation:
    raw = {"type": "message", "ts": ts, "user": user, "text": text}
    return MessageObservation(
        channel_id=channel_id,
        ts=ts,
        workspace_key=workspace_key,
        user_id=user,
        thread_ts=None,
        subtype=None,
        text=text,
        search_text=text,
        content_sha256=content_sha256(raw),
        edited_ts=None,
        is_deletion=False,
        is_partial=False,
        source="redux.messages",
        source_rank=100,
        raw=raw,
    )


def seed_eval_corpus(repo: SqliteRepository) -> list[EvalQuery]:
    """Insert labelled messages; return eval queries (synthetic IDs only)."""
    now = "2026-01-15T12:00:00Z"
    device = "EVALDEVICE0000000000000000"
    hlc = HLC(device_id=device)
    repo.upsert_device(device, "eval")
    repo.upsert_source("SRCEVAL01", device, "test", "eval", None)
    snap = "SNEVAL00000000000000000001"
    existing = repo._conn.execute(  # noqa: SLF001
        "SELECT 1 FROM snapshots WHERE snapshot_id=?", (snap,)
    ).fetchone()
    if not existing:
        repo.begin_snapshot(snap, "SRCEVAL01", "manual")

    ws = Workspace(
        workspace_key="TEVAL01",
        team_id="TEVAL01",
        enterprise_id=None,
        name="Eval Co",
        domain="evalco",
        self_user_id="UEVALSELF",
        self_is_guest=False,
        raw={"id": "TEVAL01", "name": "Eval Co"},
    )
    repo.upsert_workspace(ws, hlc.now(), now)
    for uid, name in (("UEVALSELF", "self"), ("UEVALALICE", "alice")):
        repo.upsert_user(
            User(
                user_id=uid,
                team_id="TEVAL01",
                name=name,
                real_name=name.title(),
                display_name=name.title(),
                title=None,
                email=None,
                is_bot=False,
                is_deleted=False,
                is_restricted=False,
                is_ultra_restricted=False,
                is_external=False,
                tz=None,
                raw={"id": uid},
                workspace_key="TEVAL01",
            ),
            hlc.now(),
            now,
        )
    repo.upsert_channel(
        Channel(
            channel_id="CEVALGEN01",
            workspace_key="TEVAL01",
            context_team_id="TEVAL01",
            name="eval-general",
            kind="public",
            is_ext_shared=False,
            is_archived=False,
            is_member=True,
            topic=None,
            purpose=None,
            im_user_id=None,
            raw={"id": "CEVALGEN01", "name": "eval-general"},
        ),
        hlc.now(),
        now,
    )

    docs = [
        ("1700000001.000100", "UEVALALICE", "Please review the quarterly budget before Friday."),
        ("1700000002.000100", "UEVALSELF", "Shipping the deploy rollback runbook tonight."),
        ("1700000003.000100", "UEVALALICE", "We have a customer escalation in EU region."),
        ("1700000004.000100", "UEVALSELF", "HR updated the vacation policy for 2026."),
        ("1700000005.000100", "UEVALALICE", "Lunch menus and random chatter about coffee."),
        ("1700000006.000100", "UEVALSELF", "Widgets inventory check complete."),
    ]
    for ts, user, text in docs:
        repo.ingest_observation(
            _obs("CEVALGEN01", "TEVAL01", ts, user, text),
            device_id=device,
            snapshot_id=snap,
            state_sha256="evalstate",
            seen_at=now,
            hlc=hlc.now(),
        )

    return [
        EvalQuery("finance allocation", [("CEVALGEN01", "1700000001.000100")]),
        EvalQuery("revert release", [("CEVALGEN01", "1700000002.000100")]),
        EvalQuery("angry client ticket", [("CEVALGEN01", "1700000003.000100")]),
        EvalQuery("pto guidelines", [("CEVALGEN01", "1700000004.000100")]),
        EvalQuery("widgets inventory", [("CEVALGEN01", "1700000006.000100")]),
        EvalQuery("quarterly budget", [("CEVALGEN01", "1700000001.000100")]),
    ]


def run_eval(
    repo: SqliteRepository,
    queries: list[EvalQuery] | None = None,
    *,
    k_ndcg: int = 10,
    k_recall: int = 20,
) -> dict[str, Any]:
    if queries is None:
        queries = seed_eval_corpus(repo)

    embedder = HashingEmbedder(synonym_groups=EVAL_SYNONYMS)
    SearchDocsBuilder(repo).rebuild_message_docs()
    embed_pending(repo, embedder)

    svc = SearchService(repo, embedder=embedder)

    def _metrics(mode: str) -> dict[str, float]:
        ndcgs: list[float] = []
        recalls: list[float] = []
        mrrs: list[float] = []
        for q in queries:
            hits = svc.search_messages(query=q.query, mode=mode, limit=max(k_recall, k_ndcg))
            ranked = [(h["channel_id"], h["ts"]) for h in hits["results"]]
            ndcgs.append(ndcg_at_k(ranked, q.relevant, k_ndcg))
            recalls.append(recall_at_k(ranked, q.relevant, k_recall))
            mrrs.append(mrr(ranked, q.relevant))
        n = max(len(queries), 1)
        return {
            f"ndcg@{k_ndcg}": sum(ndcgs) / n,
            f"recall@{k_recall}": sum(recalls) / n,
            "mrr": sum(mrrs) / n,
        }

    lexical = _metrics("lexical")
    hybrid = _metrics("hybrid")
    semantic = _metrics("semantic")
    lex_ndcg = lexical[f"ndcg@{k_ndcg}"]
    hyb_ndcg = hybrid[f"ndcg@{k_ndcg}"]
    if lex_ndcg <= 1e-9 and hyb_ndcg <= 1e-9:
        relative = 0.0
        passed = False
    elif lex_ndcg <= 1e-9:
        relative = float("inf")
        passed = hyb_ndcg > 0
    else:
        relative = (hyb_ndcg - lex_ndcg) / lex_ndcg
        passed = relative >= 0.10
    return {
        "lexical": lexical,
        "semantic": semantic,
        "hybrid": hybrid,
        "hybrid_vs_lexical_ndcg_relative": relative,
        "pass_hybrid_ge_lexical_plus_10pct": bool(passed),
        "queries": len(queries),
    }


def bench_hybrid_latency(
    repo: SqliteRepository,
    *,
    n_messages: int = 5_000,
    n_queries: int = 30,
    embed_limit: int | None = None,
) -> dict[str, Any]:
    """Measure hybrid latency on a synthetic corpus (HashingEmbedder; offline).

    Default ``n_messages`` is CI-friendly. Pass 200_000 for the PLAN acceptance
    laptop check via ``slackcache eval --bench``.
    """
    now = "2026-01-15T12:00:00Z"
    device = "BENCHDEVICE000000000000000"
    hlc = HLC(device_id=device)
    repo.upsert_device(device, "bench")
    repo._conn.execute(  # noqa: SLF001
        """
        INSERT OR IGNORE INTO sources(source_id, device_id, kind, path_hint, first_seen, last_seen)
        VALUES ('SRCBENCH01', ?, 'test', 'bench', ?, ?)
        """,
        (device, now, now),
    )
    snap = "SNBENCH0000000000000000001"
    repo._conn.execute(  # noqa: SLF001
        """
        INSERT OR IGNORE INTO snapshots(snapshot_id, source_id, started_at, trigger, stats_json)
        VALUES (?, 'SRCBENCH01', ?, 'manual', '{}')
        """,
        (snap, now),
    )
    repo._conn.commit()  # noqa: SLF001

    repo.upsert_workspace(
        Workspace(
            workspace_key="TBENCH01",
            team_id="TBENCH01",
            enterprise_id=None,
            name="Bench",
            domain="bench",
            self_user_id="UBENCH",
            self_is_guest=False,
            raw={"id": "TBENCH01"},
        ),
        hlc.now(),
        now,
    )
    repo.upsert_channel(
        Channel(
            channel_id="CBENCH01",
            workspace_key="TBENCH01",
            context_team_id="TBENCH01",
            name="bench",
            kind="public",
            is_ext_shared=False,
            is_archived=False,
            is_member=True,
            topic=None,
            purpose=None,
            im_user_id=None,
            raw={"id": "CBENCH01"},
        ),
        hlc.now(),
        now,
    )

    topics = [
        "budget planning notes",
        "deploy pipeline status",
        "customer ticket followup",
        "vacation calendar sync",
        "widgets stock levels",
        "random coffee chat",
    ]
    emb = HashingEmbedder()
    for i in range(n_messages):
        ts = f"{1700000000 + i}.000100"
        text = f"{topics[i % len(topics)]} item {i}"
        repo.ingest_observation(
            _obs("CBENCH01", "TBENCH01", ts, "UBENCH", text),
            device_id=device,
            snapshot_id=snap,
            state_sha256="benchstate",
            seen_at=now,
            hlc=hlc.now(),
        )

    SearchDocsBuilder(repo).rebuild_message_docs()
    cap = embed_limit if embed_limit is not None else n_messages
    emb_result = embed_pending(repo, emb, limit=cap)
    if not emb_result.get("ok"):
        return {
            "ok": False,
            "error": emb_result.get("error") or "embed_pending failed",
            "n_messages": n_messages,
            "pass_p95_under_300ms": False,
        }

    svc = SearchService(repo, embedder=emb)
    queries = [
        "budget planning",
        "deploy pipeline",
        "customer ticket",
        "vacation calendar",
        "widgets stock",
    ]
    latencies: list[float] = []
    for i in range(n_queries):
        q = queries[i % len(queries)]
        t0 = time.perf_counter()
        svc.search_messages(query=q, mode="hybrid", limit=10)
        latencies.append((time.perf_counter() - t0) * 1000.0)
    latencies.sort()
    p95 = latencies[int(0.95 * (len(latencies) - 1))]
    return {
        "ok": True,
        "n_messages": n_messages,
        "embedded": int(emb_result.get("embedded") or 0),
        "n_queries": n_queries,
        "p50_ms": latencies[len(latencies) // 2],
        "p95_ms": p95,
        "pass_p95_under_300ms": p95 < 300.0,
    }


def run_private_eval(
    archive_db: Any,
    private_path: Any,
    *,
    models_dir: Any | None = None,
) -> dict[str, Any]:
    """Evaluate private.jsonl against a consistent snapshot of the live index.

    Uses SQLite's backup API (not a raw file/WAL copy) and the active production
    embedder so metrics reflect the deployed model without mutating the archive.
    Never echoes query/ref text.
    """
    import sqlite3
    import tempfile
    from pathlib import Path

    path = Path(private_path)
    if not path.is_file():
        return {"ok": False, "error": "private.jsonl not found"}

    queries: list[EvalQuery] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        q = obj.get("query")
        rel = obj.get("relevant") or obj.get("refs") or []
        if not isinstance(q, str) or not q:
            continue
        pairs: list[tuple[str, str]] = []
        for item in rel:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                pairs.append((str(item[0]), str(item[1])))
            elif isinstance(item, dict) and item.get("channel_id") and item.get("ts"):
                pairs.append((str(item["channel_id"]), str(item["ts"])))
            elif isinstance(item, str) and item.startswith("slk://"):
                parts = item.split("/")
                if len(parts) >= 4:
                    pairs.append((parts[3], parts[4] if len(parts) > 4 else parts[-1]))
        if pairs:
            queries.append(EvalQuery(query=q, relevant=pairs))
    if not queries:
        return {"ok": False, "error": "no labelled queries", "private_queries": 0}
    src_path = Path(archive_db)
    if not src_path.exists():
        return {"ok": False, "error": "archive.db missing", "private_queries": len(queries)}

    import shutil

    from slack_cache_mcp.embeddings.factory import load_embedder

    tmp_dir = Path(tempfile.mkdtemp(prefix="slackcache-private-eval-"))
    tmp_db = tmp_dir / "archive.db"
    result: dict[str, Any] = {
        "ok": False,
        "private_queries": len(queries),
    }
    try:
        src = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(str(tmp_db))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        repo = SqliteRepository(tmp_db, read_only=False)
        try:
            embedder = load_embedder(
                models_dir=Path(models_dir) if models_dir else None
            )
            svc = SearchService(repo, embedder=embedder)
            k_ndcg, k_recall = 10, 20

            def _metrics(mode: str) -> dict[str, float]:
                ndcgs: list[float] = []
                recalls: list[float] = []
                mrrs: list[float] = []
                for q in queries:
                    hits = svc.search_messages(
                        query=q.query, mode=mode, limit=max(k_recall, k_ndcg)
                    )
                    ranked = [(h["channel_id"], h["ts"]) for h in hits["results"]]
                    ndcgs.append(ndcg_at_k(ranked, q.relevant, k_ndcg))
                    recalls.append(recall_at_k(ranked, q.relevant, k_recall))
                    mrrs.append(mrr(ranked, q.relevant))
                n = max(len(queries), 1)
                return {
                    f"ndcg@{k_ndcg}": sum(ndcgs) / n,
                    f"recall@{k_recall}": sum(recalls) / n,
                    "mrr": sum(mrrs) / n,
                }

            lexical = _metrics("lexical")
            hybrid = _metrics("hybrid")
            lex_ndcg = lexical[f"ndcg@{k_ndcg}"]
            hyb_ndcg = hybrid[f"ndcg@{k_ndcg}"]
            if lex_ndcg <= 1e-9 and hyb_ndcg <= 1e-9:
                relative = 0.0
                passed = False
            elif lex_ndcg <= 1e-9:
                relative = float("inf")
                passed = hyb_ndcg > 0
            else:
                relative = (hyb_ndcg - lex_ndcg) / lex_ndcg
                passed = relative >= 0.10
            result = {
                "ok": True,
                "private_queries": len(queries),
                "embedder": embedder.model_id,
                "lexical_ndcg": lexical.get("ndcg@10"),
                "hybrid_ndcg": hybrid.get("ndcg@10"),
                "hybrid_vs_lexical_ndcg_relative": relative,
                "pass_hybrid_ge_lexical_plus_10pct": bool(passed),
            }
        finally:
            repo.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return result
