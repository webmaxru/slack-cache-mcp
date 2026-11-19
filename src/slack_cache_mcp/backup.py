"""SQLite backup-API snapshot of archive.db (PLAN §12.2 / slackpipe pattern)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def _connect_readonly(path: Path) -> sqlite3.Connection:
    """Open SQLite read-only with a URI-safe path (spaces/special chars on Windows)."""
    # Path.as_uri() percent-encodes; append mode=ro for the sqlite URI query.
    uri = path.resolve().as_uri()
    uri = f"{uri}&mode=ro" if "?" in uri else f"{uri}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def backup_archive(archive_db: Path, backups_dir: Path) -> dict[str, Any]:
    """Copy archive.db via sqlite3 backup API + integrity_check + atomic replace."""
    archive_db = Path(archive_db)
    backups_dir = Path(backups_dir)
    if not archive_db.is_file():
        return {"ok": False, "error": "archive_missing", "path": str(archive_db)}

    backups_dir.mkdir(parents=True, exist_ok=True)
    dest = backups_dir / f"archive-{_utc_stamp()}.db"
    tmp = dest.with_suffix(".db.tmp")

    src = _connect_readonly(archive_db)
    try:
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst)
            dst.commit()
            row = dst.execute("PRAGMA integrity_check").fetchone()
            ok = row is not None and row[0] == "ok"
            if not ok:
                dst.close()
                tmp.unlink(missing_ok=True)
                return {
                    "ok": False,
                    "error": "integrity_check_failed",
                    "detail": None if row is None else row[0],
                }
        finally:
            dst.close()
    finally:
        src.close()

    tmp.replace(dest)
    return {
        "ok": True,
        "path": str(dest),
        "bytes": dest.stat().st_size,
    }
