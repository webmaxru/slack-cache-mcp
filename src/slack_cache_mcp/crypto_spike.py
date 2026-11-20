"""M2 archive DB encryption spike (PLAN §12.2).

Evaluates whether SQLCipher / SQLite3 Multiple Ciphers wheels are usable on
Windows alongside future sqlite-vec. Product default remains plaintext SQLite
protected by BitLocker + EFS/ACL + vault encryption for sanitized states.
"""

from __future__ import annotations

import sqlite3
from typing import Any


def run_encryption_spike() -> dict[str, Any]:
    """Probe available encryption backends without enabling them for archive.db."""
    result: dict[str, Any] = {
        "stdlib_sqlite": sqlite3.sqlite_version,
        "sqlcipher": {"available": False},
        "sqlite3mc_apsw": {"available": False},
        "recommendation": (
            "Defer archive.db encryption to a later milestone. Use BitLocker on the "
            "volume, ACL/EFS on %LOCALAPPDATA%\\slackcache, and AES-GCM vault objects. "
            "Revisit SQLCipher/sqlite3mc once sqlite-vec (M4) wheel compatibility is confirmed."
        ),
        "decision": "bitlocker_efs_vault_only",
    }

    # pysqlcipher3 / sqlcipher3
    for mod_name in ("pysqlcipher3.dbapi2", "sqlcipher3"):
        try:
            mod = __import__(mod_name, fromlist=["connect"])
            conn = mod.connect(":memory:")
            try:
                ver = conn.execute("PRAGMA cipher_version").fetchone()
                result["sqlcipher"] = {
                    "available": True,
                    "module": mod_name,
                    "cipher_version": ver[0] if ver else None,
                }
            finally:
                conn.close()
            break
        except Exception as exc:
            result["sqlcipher"]["probe_error"] = f"{mod_name}: {type(exc).__name__}"

    # apsw + sqlite3mc
    try:
        import apsw  # type: ignore[import-not-found]

        conn = apsw.Connection(":memory:")
        try:
            # Multiple Ciphers typically exposes pragma cipher
            rows = list(conn.execute("PRAGMA compile_options"))
            opts = [r[0] for r in rows]
            has_mc = any("CIPHER" in o.upper() or "SQLITE3MC" in o.upper() for o in opts)
            result["sqlite3mc_apsw"] = {
                "available": has_mc,
                "apsw_version": getattr(apsw, "apswversion", lambda: "?")(),
                "compile_options_sample": opts[:20],
            }
        finally:
            conn.close()
    except Exception as exc:
        result["sqlite3mc_apsw"]["probe_error"] = f"{type(exc).__name__}: {exc}"

    return result
