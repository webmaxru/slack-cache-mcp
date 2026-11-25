"""Build retrieval units (message / thread / window / canvas / file) for embeddings."""

from __future__ import annotations

import contextlib
import hashlib
from datetime import UTC, datetime
from typing import Any

from slack_cache_mcp.store.sqlite.repo import SqliteRepository

WINDOW_GAP_S = 15 * 60
WINDOW_MAX_CHARS = 1600
THREAD_MAX_CHARS = 1600


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fmt_ts(ts: str) -> str:
    try:
        dt = datetime.fromtimestamp(float(ts), tz=UTC)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return ts


def format_message_doc(
    *,
    channel_name: str | None,
    workspace_key: str,
    author: str | None,
    ts: str,
    text: str,
    thread_parent_preview: str | None = None,
) -> str:
    chan = channel_name or "?"
    who = author or "unknown"
    body = f"[#{chan} | {workspace_key}] {who} ({_fmt_ts(ts)}): {text}"
    if thread_parent_preview:
        preview = thread_parent_preview[:200]
        body += f" (reply in thread: {preview})"
    return body


class SearchDocsBuilder:
    """Materialize ``search_docs`` rows from archive tables (no embedding yet)."""

    def __init__(self, repo: SqliteRepository) -> None:
        self.repo = repo

    def rebuild_all(self) -> dict[str, int]:
        counts = {
            "message": self.rebuild_message_docs(),
            "thread": self.rebuild_thread_docs(),
            "window": self.rebuild_window_docs(),
            "canvas": self.rebuild_canvas_docs(),
            "file": self.rebuild_file_docs(),
        }
        return counts

    def upsert_message_doc(self, channel_id: str, ts: str) -> int | None:
        row = self.repo._conn.execute(  # noqa: SLF001
            "SELECT * FROM messages WHERE channel_id=? AND ts=?",
            (channel_id, ts),
        ).fetchone()
        if row is None:
            return None
        return self._upsert_from_message_row(dict(row))

    def rebuild_message_docs(self) -> int:
        rows = self.repo._conn.execute(  # noqa: SLF001
            "SELECT * FROM messages WHERE deleted_state='none'"
        ).fetchall()
        n = 0
        for r in rows:
            if self._upsert_from_message_row(dict(r)) is not None:
                n += 1
        self.repo._conn.commit()  # noqa: SLF001
        return n

    def _upsert_from_message_row(self, m: dict[str, Any]) -> int | None:
        channel_id = m["channel_id"]
        ts = m["ts"]
        chan = self.repo._conn.execute(  # noqa: SLF001
            "SELECT name FROM channels WHERE channel_id=?", (channel_id,)
        ).fetchone()
        author = None
        if m.get("user_id"):
            u = self.repo._conn.execute(  # noqa: SLF001
                "SELECT display_name, name, real_name FROM users WHERE user_id=?",
                (m["user_id"],),
            ).fetchone()
            if u:
                author = u["display_name"] or u["name"] or u["real_name"] or m["user_id"]
            else:
                author = m["user_id"]
        parent_preview = None
        if m.get("thread_ts") and m["thread_ts"] != ts:
            parent = self.repo._conn.execute(  # noqa: SLF001
                """
                SELECT text, deleted_state FROM messages
                WHERE channel_id=? AND ts=?
                """,
                (channel_id, m["thread_ts"]),
            ).fetchone()
            if parent and (parent["deleted_state"] or "none") == "none":
                parent_preview = parent["text"]
        text = format_message_doc(
            channel_name=chan["name"] if chan else None,
            workspace_key=m["workspace_key"],
            author=author,
            ts=ts,
            text=m.get("search_text") or m.get("text") or "",
            thread_parent_preview=parent_preview,
        )
        return self.repo.upsert_search_doc(
            kind="message",
            workspace_key=m["workspace_key"],
            channel_id=channel_id,
            anchor_ts=ts,
            start_ts=ts,
            end_ts=ts,
            text=text,
            message_rowid=m.get("rowid"),
        )

    def _delete_kind(self, kind: str) -> None:
        rows = self.repo._conn.execute(  # noqa: SLF001
            "SELECT doc_id FROM search_docs WHERE kind=?", (kind,)
        ).fetchall()
        ids = [int(r["doc_id"]) for r in rows]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            with contextlib.suppress(Exception):
                self.repo._conn.execute(  # noqa: SLF001
                    f"DELETE FROM docs_vec WHERE doc_id IN ({placeholders})", ids
                )
            with contextlib.suppress(Exception):
                self.repo._conn.execute(  # noqa: SLF001
                    f"DELETE FROM doc_embeddings WHERE doc_id IN ({placeholders})", ids
                )
        self.repo._conn.execute(  # noqa: SLF001
            "DELETE FROM search_docs WHERE kind=?", (kind,)
        )

    def _delete_doc_ids(self, ids: list[int]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with contextlib.suppress(Exception):
            self.repo._conn.execute(  # noqa: SLF001
                f"DELETE FROM docs_vec WHERE doc_id IN ({placeholders})", ids
            )
        with contextlib.suppress(Exception):
            self.repo._conn.execute(  # noqa: SLF001
                f"DELETE FROM doc_embeddings WHERE doc_id IN ({placeholders})", ids
            )
        self.repo._conn.execute(  # noqa: SLF001
            f"DELETE FROM search_docs WHERE doc_id IN ({placeholders})", ids
        )

    def drop_message_doc(self, channel_id: str, ts: str) -> None:
        rows = self.repo._conn.execute(  # noqa: SLF001
            """
            SELECT doc_id FROM search_docs
            WHERE kind='message' AND channel_id=? AND anchor_ts=?
            """,
            (channel_id, ts),
        ).fetchall()
        self._delete_doc_ids([int(r["doc_id"]) for r in rows])

    def invalidate_aggregates(self, channel_id: str, thread_ts: str) -> None:
        """Drop the thread doc for ``thread_ts`` (windows handled separately)."""
        rows = self.repo._conn.execute(  # noqa: SLF001
            """
            SELECT doc_id FROM search_docs
            WHERE channel_id=? AND kind='thread' AND anchor_ts=?
            """,
            (channel_id, thread_ts),
        ).fetchall()
        self._delete_doc_ids([int(r["doc_id"]) for r in rows])

    def refresh_aggregates(
        self, channel_id: str, thread_ts: str, *, message_ts: str | None = None
    ) -> None:
        """Rebuild the affected thread; refresh windows only for top-level msgs."""
        self.invalidate_aggregates(channel_id, thread_ts)
        self._upsert_thread_doc(channel_id, thread_ts)
        # Windows index only top-level messages; replies must not touch them.
        top_level = message_ts is None or message_ts == thread_ts
        if top_level:
            self._rebuild_windows_around(channel_id, message_ts or thread_ts)

    def refresh_reply_docs_after_parent_change(
        self, channel_id: str, parent_ts: str
    ) -> None:
        """Re-upsert reply message docs so deleted parents leave no preview."""
        replies = self.repo._conn.execute(  # noqa: SLF001
            """
            SELECT ts FROM messages
            WHERE channel_id=? AND thread_ts=? AND ts!=? AND deleted_state='none'
            """,
            (channel_id, parent_ts, parent_ts),
        ).fetchall()
        for r in replies:
            self.upsert_message_doc(channel_id, r["ts"])

    def _upsert_thread_doc(self, channel_id: str, thread_ts: str) -> None:
        msgs = self.repo._conn.execute(  # noqa: SLF001
            """
            SELECT * FROM messages
            WHERE channel_id=? AND (ts=? OR thread_ts=?) AND deleted_state='none'
            ORDER BY ts_num ASC
            """,
            (channel_id, thread_ts, thread_ts),
        ).fetchall()
        if not msgs:
            return
        parts: list[str] = []
        total = 0
        for m in msgs:
            chunk = f"{m['user_id'] or '?'}: {m['search_text'] or m['text']}"
            if total + len(chunk) > THREAD_MAX_CHARS and parts:
                break
            parts.append(chunk)
            total += len(chunk) + 1
        text = "\n".join(parts)
        self.repo.upsert_search_doc(
            kind="thread",
            workspace_key=msgs[0]["workspace_key"],
            channel_id=channel_id,
            anchor_ts=thread_ts,
            start_ts=msgs[0]["ts"],
            end_ts=msgs[-1]["ts"],
            text=text,
        )

    def _rebuild_windows_around(self, channel_id: str, focus_ts: str) -> None:
        """Rebuild windows overlapping ``focus_ts`` and adjacent boundary buckets."""
        ch = self.repo._conn.execute(  # noqa: SLF001
            """
            SELECT DISTINCT channel_id, workspace_key FROM messages
            WHERE channel_id=? AND deleted_state='none'
            """,
            (channel_id,),
        ).fetchone()
        if not ch:
            return
        try:
            focus_num = float(focus_ts)
        except ValueError:
            return
        # Drop windows that overlap focus or sit within one gap (boundary merges).
        win_rows = self.repo._conn.execute(  # noqa: SLF001
            """
            SELECT doc_id, start_ts, end_ts FROM search_docs
            WHERE channel_id=? AND kind='window'
              AND CAST(start_ts AS REAL) <= ?
              AND CAST(end_ts AS REAL) >= ?
            """,
            (channel_id, focus_num + WINDOW_GAP_S, focus_num - WINDOW_GAP_S),
        ).fetchall()
        range_lo = focus_num
        range_hi = focus_num
        for w in win_rows:
            with contextlib.suppress(TypeError, ValueError):
                range_lo = min(range_lo, float(w["start_ts"]))
                range_hi = max(range_hi, float(w["end_ts"]))
        self._delete_doc_ids([int(w["doc_id"]) for w in win_rows])
        msgs = self.repo._conn.execute(  # noqa: SLF001
            """
            SELECT * FROM messages
            WHERE channel_id=? AND deleted_state='none'
              AND (thread_ts IS NULL OR thread_ts=ts)
              AND ts_num >= ? AND ts_num <= ?
            ORDER BY ts_num ASC
            """,
            (channel_id, range_lo - WINDOW_GAP_S, range_hi + WINDOW_GAP_S),
        ).fetchall()
        if not msgs:
            return
        # Extend to full adjacent buckets by including neighbors within gap of range.
        bucket: list[Any] = []
        flushed_any = False
        for m in msgs:
            if bucket:
                prev_ts = float(bucket[-1]["ts_num"])
                gap = float(m["ts_num"]) - prev_ts
                size = sum(len(x["search_text"] or x["text"] or "") for x in bucket)
                if gap > WINDOW_GAP_S or size > WINDOW_MAX_CHARS:
                    b_lo, b_hi = float(bucket[0]["ts_num"]), float(bucket[-1]["ts_num"])
                    if b_hi >= range_lo - WINDOW_GAP_S and b_lo <= range_hi + WINDOW_GAP_S:
                        self._flush_window(ch["workspace_key"], channel_id, bucket)
                        flushed_any = True
                    bucket = []
            bucket.append(m)
        if bucket:
            b_lo, b_hi = float(bucket[0]["ts_num"]), float(bucket[-1]["ts_num"])
            if b_hi >= range_lo - WINDOW_GAP_S and b_lo <= range_hi + WINDOW_GAP_S:
                self._flush_window(ch["workspace_key"], channel_id, bucket)
                flushed_any = True
        # Message with no prior window still needs a bucket (endpoint delete / first msg).
        if not flushed_any and msgs:
            # Find the natural bucket containing focus among full channel scan.
            all_msgs = self.repo._conn.execute(  # noqa: SLF001
                """
                SELECT * FROM messages
                WHERE channel_id=? AND deleted_state='none'
                  AND (thread_ts IS NULL OR thread_ts=ts)
                ORDER BY ts_num ASC
                """,
                (channel_id,),
            ).fetchall()
            bucket = []
            for m in all_msgs:
                if bucket:
                    prev_ts = float(bucket[-1]["ts_num"])
                    gap = float(m["ts_num"]) - prev_ts
                    size = sum(len(x["search_text"] or x["text"] or "") for x in bucket)
                    if gap > WINDOW_GAP_S or size > WINDOW_MAX_CHARS:
                        if float(bucket[0]["ts_num"]) <= focus_num <= float(
                            bucket[-1]["ts_num"]
                        ):
                            self._flush_window(ch["workspace_key"], channel_id, bucket)
                            return
                        bucket = []
                bucket.append(m)
            if bucket and float(bucket[0]["ts_num"]) <= focus_num <= float(
                bucket[-1]["ts_num"]
            ):
                self._flush_window(ch["workspace_key"], channel_id, bucket)

    def rebuild_thread_docs(self) -> int:
        self._delete_kind("thread")
        threads = self.repo._conn.execute(  # noqa: SLF001
            """
            SELECT channel_id, thread_ts FROM messages
            WHERE thread_ts IS NOT NULL AND deleted_state='none'
            GROUP BY channel_id, thread_ts
            """
        ).fetchall()
        n = 0
        for t in threads:
            msgs = self.repo._conn.execute(  # noqa: SLF001
                """
                SELECT * FROM messages
                WHERE channel_id=? AND (ts=? OR thread_ts=?) AND deleted_state='none'
                ORDER BY ts_num ASC
                """,
                (t["channel_id"], t["thread_ts"], t["thread_ts"]),
            ).fetchall()
            if not msgs:
                continue
            parts: list[str] = []
            total = 0
            for m in msgs:
                chunk = f"{m['user_id'] or '?'}: {m['search_text'] or m['text']}"
                if total + len(chunk) > THREAD_MAX_CHARS and parts:
                    break
                parts.append(chunk)
                total += len(chunk) + 1
            text = "\n".join(parts)
            ws = msgs[0]["workspace_key"]
            self.repo.upsert_search_doc(
                kind="thread",
                workspace_key=ws,
                channel_id=t["channel_id"],
                anchor_ts=t["thread_ts"],
                start_ts=msgs[0]["ts"],
                end_ts=msgs[-1]["ts"],
                text=text,
            )
            n += 1
        self.repo._conn.commit()  # noqa: SLF001
        return n

    def rebuild_window_docs(self) -> int:
        self._delete_kind("window")
        channels = self.repo._conn.execute(  # noqa: SLF001
            "SELECT DISTINCT channel_id, workspace_key FROM messages WHERE deleted_state='none'"
        ).fetchall()
        n = 0
        for ch in channels:
            msgs = self.repo._conn.execute(  # noqa: SLF001
                """
                SELECT * FROM messages
                WHERE channel_id=? AND deleted_state='none'
                  AND (thread_ts IS NULL OR thread_ts=ts)
                ORDER BY ts_num ASC
                """,
                (ch["channel_id"],),
            ).fetchall()
            if not msgs:
                continue
            bucket: list[Any] = []
            for m in msgs:
                if bucket:
                    prev_ts = float(bucket[-1]["ts_num"])
                    gap = float(m["ts_num"]) - prev_ts
                    size = sum(len(x["search_text"] or x["text"] or "") for x in bucket)
                    if gap > WINDOW_GAP_S or size > WINDOW_MAX_CHARS:
                        n += self._flush_window(ch["workspace_key"], ch["channel_id"], bucket)
                        bucket = []
                bucket.append(m)
            if bucket:
                n += self._flush_window(ch["workspace_key"], ch["channel_id"], bucket)
        self.repo._conn.commit()  # noqa: SLF001
        return n

    def _flush_window(self, workspace_key: str, channel_id: str, msgs: list[Any]) -> int:
        text = "\n".join(f"{m['user_id'] or '?'}: {m['search_text'] or m['text']}" for m in msgs)
        self.repo.upsert_search_doc(
            kind="window",
            workspace_key=workspace_key,
            channel_id=channel_id,
            anchor_ts=msgs[0]["ts"],
            start_ts=msgs[0]["ts"],
            end_ts=msgs[-1]["ts"],
            text=text[: WINDOW_MAX_CHARS * 2],
        )
        return 1

    def rebuild_canvas_docs(self) -> int:
        self._delete_kind("canvas")
        rows = self.repo._conn.execute(  # noqa: SLF001
            "SELECT * FROM canvases"
        ).fetchall()
        n = 0
        for r in rows:
            title = r["title"] or ""
            body = r["text"] or ""
            text = f"{title}\n{body}".strip()
            if not text:
                continue
            self.repo.upsert_search_doc(
                kind="canvas",
                workspace_key=r["workspace_key"] or "unknown",
                channel_id=r["channel_id"],
                anchor_ts=r["canvas_id"],
                start_ts=None,
                end_ts=None,
                text=text,
            )
            n += 1
        self.repo._conn.commit()  # noqa: SLF001
        return n

    def rebuild_file_docs(self) -> int:
        self._delete_kind("file")
        rows = self.repo._conn.execute("SELECT * FROM files").fetchall()  # noqa: SLF001
        n = 0
        for r in rows:
            parts = [
                p
                for p in (
                    r["title"],
                    r["name"],
                    r["preview_text"],
                    r["ocr_text"],
                )
                if p
            ]
            text = "\n".join(parts).strip()
            if not text:
                continue
            self.repo.upsert_search_doc(
                kind="file",
                workspace_key=r["workspace_key"] or "unknown",
                channel_id=None,
                anchor_ts=r["file_id"],
                start_ts=None,
                end_ts=None,
                text=text,
            )
            n += 1
        self.repo._conn.commit()  # noqa: SLF001
        return n
