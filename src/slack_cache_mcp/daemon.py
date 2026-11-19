"""Continuous sync engine: watcher + 60s poll + 15min full pass (PLAN §5.3)."""

from __future__ import annotations

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


def _watch_roots(profile: Path) -> list[Path]:
    origin = idb_origin_dir_name()
    roots = [
        profile / "IndexedDB" / f"{origin}.blob",
        profile / "IndexedDB" / f"{origin}.leveldb",
        profile / "Local Storage" / "leveldb",
        profile / "storage",
    ]
    return [r for r in roots if r.exists()]


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

        profiles = discover_profiles(overrides=settings.source_overrides or None)
        valid = [p for p in profiles if p.valid]
        observer = Observer()
        watched = 0
        for p in valid:
            for root in _watch_roots(p.path):
                observer.schedule(Handler(), str(root), recursive=True)
                watched += 1
        if watched:
            observer.start()
        log_event(logger, "daemon_start", watched_roots=watched, profiles=len(valid))
    except Exception as exc:
        log_event(logger, "watcher_unavailable", error=type(exc).__name__)
        observer = None

    # Initial full pass
    _run("full")
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
    finally:
        debouncer.cancel()
        if observer is not None:
            observer.stop()
            observer.join(timeout=5)
        if ensure_single:
            release()
        log_event(logger, "daemon_stop", **metrics)

    return {"ok": True, "metrics": metrics}
