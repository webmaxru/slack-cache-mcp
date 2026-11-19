"""Structured JSON daemon logs: counts, hashed IDs, durations — never message text/tokens."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any


def hash_id(value: str, *, prefix: str = "") -> str:
    """Short irreversible id for logs (e.g. team#3f2a)."""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:4]
    label = prefix or value[:4]
    return f"{label}#{digest}"


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_daemon_logging(logs_dir: Path, *, level: int = logging.INFO) -> logging.Logger:
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("slackcache.daemon")
    logger.setLevel(level)
    logger.handlers.clear()
    logger.propagate = False
    path = logs_dir / "daemon.log"
    handler = TimedRotatingFileHandler(
        path,
        when="midnight",
        backupCount=14,
        encoding="utf-8",
        utc=True,
    )
    handler.namer = lambda name: name.replace(".log", "") + ".log"  # keep *.log
    # TimedRotatingFileHandler renames to daemon.log.YYYY-MM-DD; PLAN wants daemon-YYYYMMDD.log
    handler.suffix = "%Y%m%d"
    handler.setFormatter(JsonLineFormatter())
    logger.addHandler(handler)
    # Also mirror to stderr for interactive runs
    stream = logging.StreamHandler()
    stream.setFormatter(JsonLineFormatter())
    logger.addHandler(stream)
    return logger


def log_event(logger: logging.Logger, msg: str, **fields: Any) -> None:
    logger.info(msg, extra={"extra_fields": fields})
