"""Continuous sync engine: watcher + 60s poll + 15min full pass (PLAN §5.3)."""

from __future__ import annotations

import contextlib
import os
import threading
import time
from pathlib import Path
from typing import Any

from slack_cache_mcp.config import Settings
from slack_cache_mcp.desktop.chromium.idb_values import idb_origin_dir_name
from slack_cache_mcp.desktop.locate import discover_profiles
from slack_cache_mcp.logutil import log_event, setup_daemon_logging
from slack_cache_mcp.pipeline import run_snapshot_cycle
from slack_cache_mcp.single_instance import AlreadyRunningError, acquire, release
from slack_cache_mcp.vault.store import VaultStore

POLL_INTERVAL_S = 60.0
FULL_PASS_INTERVAL_S = 15 * 60.0
WATCH_DEBOUNCE_S = 2.0


class _DebouncedTrigger:
    def __init__(self, delay_s: float, on_fire: Any) -> None:
        self.delay_s = delay_s
        self.on_fire = on_fire
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def ping(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.delay_s, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        self.on_fire()

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None


def _watch_roots(profile: Path, *, edge_browser: bool = False) -> list[Path]:
    origin = idb_origin_dir_name()
    roots = [
        profile / "IndexedDB" / f"{origin}.blob",
        profile / "IndexedDB" / f"{origin}.leveldb",
    ]
    if not edge_browser:
        # Desktop-only: never watch Edge Local Storage / root-state (credential-adjacent).
        roots.extend(
            [
                profile / "Local Storage" / "leveldb",
                profile / "storage",
            ]
        )
    return [r for r in roots if r.exists() and not r.is_symlink()]


def _watch_root_key(root: Path) -> str:
    try:
        return str(root.resolve())
    except OSError:
        return str(root)


def run_daemon(
    settings: Settings,
    *,
    ensure_single: bool = True,
    max_cycles: int | None = None,
    poll_interval_s: float = POLL_INTERVAL_S,
    full_pass_interval_s: float = FULL_PASS_INTERVAL_S,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Run the continuous sync loop. ``max_cycles`` is for tests."""
    logger = setup_daemon_logging(settings.logs_dir)
    if ensure_single:
        try:
            acquire(lock_dir=settings.data_dir)
        except AlreadyRunningError:
            log_event(logger, "daemon_already_running")
            return {"ok": False, "error": "already_running"}

    stop = stop_event or threading.Event()
    cycle_lock = threading.Lock()
    metrics: dict[str, Any] = {
        "cycles": 0,
        "watch_triggers": 0,
        "poll_triggers": 0,
        "full_triggers": 0,
        "states_decoded": 0,
        "generations_new": 0,
        "messages_ingested": 0,
        "errors": 0,
    }
    last_full = 0.0

    def _run(trigger: str) -> None:
        nonlocal last_full
        if stop.is_set():
            return
        with cycle_lock:
            if stop.is_set():
                return
            t0 = time.monotonic()
            try:
                result = run_snapshot_cycle(settings, trigger=trigger)
                metrics["cycles"] += 1
                metrics["states_decoded"] += int(result.get("states_decoded") or 0)
                metrics["generations_new"] += int(result.get("generations_new") or 0)
                metrics["messages_ingested"] += int(result.get("messages_ingested") or 0)
                if trigger == "watch":
                    metrics["watch_triggers"] += 1
                elif trigger == "poll":
                    metrics["poll_triggers"] += 1
                elif trigger == "full":
                    metrics["full_triggers"] += 1
                    last_full = time.monotonic()
                    # Retention sweep on full pass
                    vault = VaultStore(
                        settings.vault_dir,
                        data_dir=settings.data_dir,
                        keep=settings.vault_keep,
                        keep_days=settings.vault_keep_days,
                    )
                    purged = vault.purge_expired()
                    log_event(logger, "vault_purge", purged=purged)
                    # Bounded embedding drain (message docs already upserted on ingest).
                    repo = None
                    try:
                        from slack_cache_mcp.embeddings.factory import load_embedder
                        from slack_cache_mcp.search.worker import embed_pending
                        from slack_cache_mcp.store.sqlite.repo import SqliteRepository

                        # Reload each full pass so a newly fetched FastEmbed
                        # model replaces hashing — but never demote a working
                        # non-hashing model to hashing (would clear the index).
                        # Also check the *persisted* archive model so a daemon
                        # restart with a temporary ONNX load failure cannot wipe
                        # the production index.
                        repo = SqliteRepository(settings.archive_db, read_only=False)
                        persisted_model = None
                        with contextlib.suppress(Exception):
                            prow = repo._conn.execute(  # noqa: SLF001
                                "SELECT value FROM meta WHERE key='embedding_model'"
                            ).fetchone()
                            if prow:
                                persisted_model = str(prow["value"])
                        fresh = load_embedder(models_dir=settings.models_dir)
                        prev = getattr(_run, "_embedder", None)
                        demote_mem = (
                            prev is not None
                            and not str(getattr(prev, "model_id", "")).startswith(
                                "hashing"
                            )
                            and str(fresh.model_id).startswith("hashing")
                        )
                        demote_disk = (
                            persisted_model is not None
                            and not persisted_model.startswith("hashing")
                            and str(fresh.model_id).startswith("hashing")
                        )
                        if demote_mem:
                            _run._embedder = prev  # type: ignore[attr-defined]
                        elif demote_disk:
                            # Skip embedding this pass; keep trying next cycle.
                            _run._embedder = None  # type: ignore[attr-defined]
                            log_event(
                                logger,
                                "embed_skip_hashing_demotion",
                                persisted_model=persisted_model,
                            )
                        else:
                            _run._embedder = fresh  # type: ignore[attr-defined]
                        # Acquire the same leased op lock as CLI rebuild so a
                        # daemon already mid-cycle cannot embed/promote under us.
                        lock_owner = f"daemon-{os.getpid()}"
                        if not repo.acquire_embedding_op_lock(
                            lock_owner, lease_s=900.0
                        ):
                            log_event(logger, "embed_skip_op_lock")
                        else:
                            try:
                                # Drain deferred channel window rebuilds.
                                try:
                                    from slack_cache_mcp.search.docs import (
                                        SearchDocsBuilder,
                                    )

                                    rebuilt = (
                                        SearchDocsBuilder(repo).rebuild_dirty_windows()
                                    )
                                    if rebuilt:
                                        log_event(
                                            logger,
                                            "window_docs_rebuild",
                                            channels=len(rebuilt),
                                            windows=sum(rebuilt.values()),
                                        )
                                except Exception as win_exc:
                                    log_event(
                                        logger,
                                        "window_docs_rebuild_error",
                                        error=type(win_exc).__name__,
                                    )
                                    with contextlib.suppress(Exception):
                                        repo._conn.rollback()  # noqa: SLF001
                                # One-time backfill: upgraded archives may have empty
                                # search_docs while messages already exist.
                                try:
                                    n_docs = int(
                                        repo._conn.execute(  # noqa: SLF001
                                            "SELECT COUNT(*) AS c FROM search_docs"
                                        ).fetchone()["c"]
                                    )
                                    n_msgs = int(
                                        repo._conn.execute(  # noqa: SLF001
                                            "SELECT COUNT(*) AS c FROM messages"
                                        ).fetchone()["c"]
                                    )
                                    flag = repo._conn.execute(  # noqa: SLF001
                                        "SELECT value FROM meta WHERE key='search_docs_backfill'"
                                    ).fetchone()
                                    if n_msgs > 0 and (n_docs == 0 or not flag):
                                        from slack_cache_mcp.search.docs import (
                                            SearchDocsBuilder,
                                        )

                                        counts = SearchDocsBuilder(repo).rebuild_all()
                                        repo._conn.execute(  # noqa: SLF001
                                            """
                                            INSERT INTO meta(key, value) VALUES ('search_docs_backfill', '1')
                                            ON CONFLICT(key) DO UPDATE SET value=excluded.value
                                            """
                                        )
                                        repo._conn.commit()  # noqa: SLF001
                                        log_event(
                                            logger,
                                            "search_docs_backfill",
                                            **{k: int(v) for k, v in counts.items()},
                                        )
                                except Exception as bf_exc:
                                    log_event(
                                        logger,
                                        "search_docs_backfill_error",
                                        error=type(bf_exc).__name__,
                                    )
                                    with contextlib.suppress(Exception):
                                        repo._conn.rollback()  # noqa: SLF001
                                active = getattr(_run, "_embedder", None)
                                if active is None:
                                    emb_result = {
                                        "ok": False,
                                        "embedded": 0,
                                        "error": "embedder_unavailable",
                                    }
                                else:
                                    if not repo.renew_embedding_op_lock(
                                        lock_owner, lease_s=900.0
                                    ):
                                        emb_result = {
                                            "ok": False,
                                            "embedded": 0,
                                            "error": "embedding_op_lock_lost",
                                        }
                                    else:
                                        emb_result = embed_pending(
                                            repo,
                                            active,
                                            limit=2_048,
                                            lock_owner=lock_owner,
                                        )
                                log_event(
                                    logger,
                                    "embed_batch",
                                    embedded=emb_result.get("embedded"),
                                    model=emb_result.get("model_id"),
                                    ok=emb_result.get("ok"),
                                )
                            finally:
                                # Clear any aborted txn so lease release can commit.
                                with contextlib.suppress(Exception):
                                    repo._conn.rollback()  # noqa: SLF001
                                with contextlib.suppress(Exception):
                                    repo.release_embedding_op_lock(lock_owner)
                    except Exception as emb_exc:
                        log_event(
                            logger,
                            "embed_batch_error",
                            error=type(emb_exc).__name__,
                        )
                    finally:
                        if repo is not None:
                            repo.close()
                log_event(
                    logger,
                    "cycle_ok",
                    trigger=trigger,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    states_decoded=result.get("states_decoded"),
                    generations_new=result.get("generations_new"),
                    messages_ingested=result.get("messages_ingested"),
                    files_copied=result.get("files_copied"),
                )
            except Exception as exc:
                metrics["errors"] += 1
                log_event(
                    logger,
                    "cycle_error",
                    trigger=trigger,
                    error=type(exc).__name__,
                )

    def _request_watch() -> None:
        _run("watch")

    debouncer = _DebouncedTrigger(WATCH_DEBOUNCE_S, _request_watch)
    observer = None
    watched_keys: set[str] = set()
    handler: Any = None
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event: Any) -> None:  # noqa: ANN401
                if getattr(event, "is_directory", False):
                    return
                src = str(getattr(event, "src_path", "") or "")
                if src.endswith("LOCK") or src.endswith("\\LOCK") or src.endswith("/LOCK"):
                    return
                debouncer.ping()

        handler = Handler()
        observer = Observer()
        log_event(logger, "daemon_start", watched_roots=0, profiles=0)
    except Exception as exc:
        log_event(logger, "watcher_unavailable", error=type(exc).__name__)
        observer = None

    def _reconcile_watchers() -> int:
        """Schedule watchers for newly discovered profiles / Slack IDB dirs."""
        if observer is None or handler is None:
            return 0
        profiles = discover_profiles(overrides=settings.source_overrides or None)
        valid = [p for p in profiles if p.valid]
        added = 0
        for p in valid:
            for root in _watch_roots(p.path, edge_browser=p.is_edge_browser):
                key = _watch_root_key(root)
                if key in watched_keys:
                    continue
                try:
                    observer.schedule(handler, str(root), recursive=True)
                except Exception as exc:  # noqa: BLE001
                    log_event(
                        logger,
                        "watcher_schedule_error",
                        error=type(exc).__name__,
                        root=str(root),
                    )
                    continue
                watched_keys.add(key)
                added += 1
        if added:
            with contextlib.suppress(RuntimeError):
                # Idempotent if already running.
                if not observer.is_alive():
                    observer.start()
            log_event(
                logger,
                "watcher_reconcile",
                added=added,
                watched_roots=len(watched_keys),
                profiles=len(valid),
            )
        return added

    # Seed watchers for profiles valid at start, then run the first full pass.
    _reconcile_watchers()
    _run("full")
    # Edge Slack IDB may appear after the first ingest; pick up new roots.
    _reconcile_watchers()
    last_full = time.monotonic()

    try:
        while not stop.is_set():
            if max_cycles is not None and metrics["cycles"] >= max_cycles:
                break
            # Sleep in small slices so stop_event is responsive
            deadline = time.monotonic() + poll_interval_s
            while time.monotonic() < deadline and not stop.is_set():
                time.sleep(0.2)
            if stop.is_set():
                break
            if max_cycles is not None and metrics["cycles"] >= max_cycles:
                break
            now = time.monotonic()
            if now - last_full >= full_pass_interval_s:
                _run("full")
            else:
                _run("poll")
            # Profiles / Slack IndexedDB dirs can appear between polls (e.g. first Edge open).
            _reconcile_watchers()
    finally:
        debouncer.cancel()
        if observer is not None:
            observer.stop()
            observer.join(timeout=5)
        if ensure_single:
            release()
        log_event(logger, "daemon_stop", **metrics)

    return {"ok": True, "metrics": metrics}
