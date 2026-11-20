"""Offline doctor / health report."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from slack_cache_mcp import netguard
from slack_cache_mcp.config import Settings
from slack_cache_mcp.desktop.chromium.idb_values import decode_blob_file, idb_origin_dir_name
from slack_cache_mcp.desktop.locate import discover_profiles, redact_path


def _detect_idb_backend(profile: Path) -> str:
    origin = idb_origin_dir_name()
    idb = profile / "IndexedDB"
    if not idb.is_dir():
        return "missing"
    for p in idb.rglob("*"):
        if not p.is_file():
            continue
        try:
            with open(p, "rb") as fh:
                head = fh.read(16)
            if head.startswith(b"SQLite format 3"):
                return "sqlite"
        except OSError:
            continue
    if (idb / f"{origin}.leveldb").is_dir():
        return "leveldb"
    return "unknown"


def run_doctor(settings: Settings) -> dict[str, Any]:
    netguard.install()
    profiles = discover_profiles(overrides=settings.source_overrides or None)
    blob_versions: dict[str, int] = {}
    decode_ok = 0
    decode_err = 0
    unknown_slices: list[str] = []
    backend = None
    for p in profiles:
        if not p.valid:
            continue
        backend = _detect_idb_backend(p.path)
        blob_root = p.path / "IndexedDB" / f"{idb_origin_dir_name()}.blob"
        if blob_root.is_dir():
            for bf in blob_root.rglob("*"):
                if not bf.is_file():
                    continue
                info, val = decode_blob_file(bf)
                key = f"blink={info.blink_version},v8={info.v8_version}"
                blob_versions[key] = blob_versions.get(key, 0) + 1
                if info.error or val is None:
                    decode_err += 1
                else:
                    decode_ok += 1
                    if isinstance(val, dict):
                        # record a few unexpected top-level keys vs known core set
                        known = {
                            "channels",
                            "members",
                            "messages",
                            "threads",
                            "teams",
                            "bootData",
                            "selfTeamIds",
                            "files",
                        }
                        for k in val:
                            if k not in known and k not in unknown_slices:
                                unknown_slices.append(str(k))
        break  # primary-ish first valid

    vault_objects = 0
    if settings.vault_dir.is_dir():
        vault_objects = sum(1 for _ in settings.vault_dir.rglob("*.zst.enc"))

    archive_exists = settings.archive_db.is_file()
    return {
        "profiles": [
            {
                "kind": p.kind,
                "path": redact_path(p.path),
                "valid": p.valid,
                "primary": p.primary,
            }
            for p in profiles
        ],
        "indexeddb_backend": backend,
        "blob_version_histogram": blob_versions,
        "decode_ok": decode_ok,
        "decode_errors": decode_err,
        "unknown_slices_sample": unknown_slices[:30],
        "netguard_installed": netguard.is_installed(),
        "data_dir": redact_path(settings.data_dir),
        "archive_db_present": archive_exists,
        "vault_objects": vault_objects,
        "vault_keep": settings.vault_keep,
        "vault_keep_days": settings.vault_keep_days,
    }
