"""Offline doctor / health report."""

from __future__ import annotations

from typing import Any

from slack_cache_mcp import netguard
from slack_cache_mcp.config import Settings
from slack_cache_mcp.desktop.chromium.httpcache import inventory_http_cache
from slack_cache_mcp.desktop.chromium.idb_backend import detect_idb_backend
from slack_cache_mcp.desktop.chromium.idb_values import decode_blob_file, idb_origin_dir_name
from slack_cache_mcp.desktop.locate import discover_profiles, redact_path
from slack_cache_mcp.desktop.slack import localstate, redux, session_scan
from slack_cache_mcp.desktop.snapshot import _resolved_under


def run_doctor(settings: Settings) -> dict[str, Any]:
    netguard.install()
    profiles = discover_profiles(overrides=settings.source_overrides or None)
    blob_versions: dict[str, int] = {}
    decode_ok = 0
    decode_err = 0
    unknown_slices: list[str] = []
    backend = None
    session_inv: dict[str, Any] = {"present": False}
    local_keys: list[str] = []
    http_inv: dict[str, Any] = {"present": False}
    # Prefer a desktop profile for inventories; Edge is Slack-IDB-only.
    ordered = sorted(
        [p for p in profiles if p.valid],
        key=lambda p: (p.is_edge_browser, not p.primary),
    )
    for p in ordered:
        edge_only = p.is_edge_browser
        backend = detect_idb_backend(p.path, slack_origin_only=edge_only)
        blob_root = p.path / "IndexedDB" / f"{idb_origin_dir_name()}.blob"
        # Reject symlink/junction Slack blob roots; never follow escapes under the tree.
        if blob_root.is_dir() and _resolved_under(blob_root, blob_root.parent):
            for bf in blob_root.rglob("*"):
                if not bf.is_file() or not _resolved_under(bf, blob_root):
                    continue
                info, val = decode_blob_file(bf)
                key = f"blink={info.blink_version},v8={info.v8_version}"
                blob_versions[key] = blob_versions.get(key, 0) + 1
                if info.error or val is None:
                    decode_err += 1
                else:
                    decode_ok += 1
                    if isinstance(val, dict):
                        for k in redux.unknown_slices(val):
                            if k not in unknown_slices:
                                unknown_slices.append(k)
        if edge_only:
            session_inv = {"present": False, "skipped": "edge_idb_only"}
            local_keys = []
            http_inv = {"present": False, "skipped": "edge_idb_only"}
        else:
            session_inv = session_scan.inventory_session_storage(p.path)
            local_keys = localstate.inventory_local_storage_keys(p.path)
            http_inv = inventory_http_cache(p.path)
        break

    vault_objects = 0
    if settings.vault_dir.is_dir():
        vault_objects = sum(1 for _ in settings.vault_dir.rglob("*.zst.enc"))

    archive_exists = settings.archive_db.is_file()
    source_yield: list[dict[str, Any]] = []
    if archive_exists:
        try:
            from slack_cache_mcp.store.sqlite.repo import SqliteRepository

            repo = SqliteRepository(settings.archive_db, read_only=True)
            source_yield = repo.source_yield_report()
            repo.close()
        except Exception:  # noqa: BLE001
            source_yield = []

    edge_profiles = [p for p in profiles if p.is_edge_browser]
    return {
        "profiles": [
            {
                "kind": p.kind,
                "path": redact_path(p.path),
                "valid": p.valid,
                "primary": p.primary,
                "edge_idb_only": p.is_edge_browser,
            }
            for p in profiles
        ],
        "edge_sources": len(edge_profiles),
        "indexeddb_backend": backend,
        "blob_version_histogram": blob_versions,
        "decode_ok": decode_ok,
        "decode_errors": decode_err,
        "unknown_slices_sample": unknown_slices[:30],
        "session_storage": session_inv,
        "local_storage_key_patterns": local_keys,
        "http_cache": http_inv,
        "source_yield": source_yield,
        "opt_in": {
            "drafts": settings.capture_drafts,
            "downloads": settings.capture_downloads,
            "http_cache": settings.capture_http_cache,
            "notifications": settings.capture_notifications,
            "ocr": settings.capture_ocr,
        },
        "netguard_installed": netguard.is_installed(),
        "data_dir": redact_path(settings.data_dir),
        "archive_db_present": archive_exists,
        "vault_objects": vault_objects,
        "vault_keep": settings.vault_keep,
        "vault_keep_days": settings.vault_keep_days,
    }
