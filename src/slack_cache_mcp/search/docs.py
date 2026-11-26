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

    def upsert_message_doc(self, channel_id: str, ts: str) -> tuple[int | None, bool]:
        """Upsert message retrieval doc. Returns ``(doc_id, text_changed)``."""
        row = self.repo._conn.execute(  # noqa: SLF001
            "SELECT * FROM messages WHERE channel_id=? AND ts=?",
            (channel_id, ts),
        ).fetchone()
        if row is None:
            return None, False
        return self._upsert_from_message_row(dict(row))

    def rebuild_message_docs(self) -> int:
        rows = self.repo._conn.execute(  # noqa: SLF001
            "SELECT * FROM messages WHERE deleted_state='none'"
        ).fetchall()
        n = 0
        for r in rows:
            doc_id, _changed = self._upsert_from_message_row(dict(r))
            if doc_id is not None:
                n += 1
        self.repo._conn.commit()  # noqa: SLF001
        return n

    def _upsert_from_message_row(self, m: dict[str, Any]) -> tuple[int | None, bool]:
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
        text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        existing = self.repo._conn.execute(  # noqa: SLF001
            """
            SELECT doc_id, text_sha256 FROM search_docs
            WHERE kind='message' AND workspace_key=? AND channel_id IS ?
              AND anchor_ts IS ? AND start_ts IS ? AND end_ts IS ?
            """,
            (m["workspace_key"], channel_id, ts, ts, ts),
        ).fetchone()
        changed = existing is None or existing["text_sha256"] != text_sha
        doc_id = self.repo.upsert_search_doc(
            kind="message",
            workspace_key=m["workspace_key"],
            channel_id=channel_id,
            anchor_ts=ts,
            start_ts=ts,
            end_ts=ts,
            text=text,
            message_rowid=m.get("rowid"),
        )
        return doc_id, changed

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
        self,
        channel_id: str,
        thread_ts: str,
        *,
        message_ts: str | None = None,
        text_changed: bool = True,
        previous_thread_ts: str | None = None,
        is_new: bool = False,
    ) -> None:
        """Rebuild affected thread docs; drop stale windows immediately."""
        focus = message_ts or thread_ts
        top_level_now = message_ts is None or message_ts == thread_ts

        if is_new:
            # Brand-new message: never treat it as a former standalone root
            # (that would create a phantom thread doc on the reply ts).
            if text_changed or top_level_now:
                self.invalidate_aggregates(channel_id, thread_ts)
                self._upsert_thread_doc(channel_id, thread_ts)
            else:
                exists = self.repo._conn.execute(  # noqa: SLF001
                    """
                    SELECT 1 FROM search_docs
                    WHERE channel_id=? AND kind='thread' AND anchor_ts=?
                    """,
                    (channel_id, thread_ts),
                ).fetchone()
                if not exists:
                    self._upsert_thread_doc(channel_id, thread_ts)
            # Only top-level messages participate in conversation windows.
            if top_level_now:
                self._invalidate_and_rebuild_windows_around(channel_id, focus)
                self.repo.mark_windows_dirty(channel_id)
            return

        # NULL previous_thread_ts means the message was previously standalone
        # (its own root). Treat that as old_root == focus.
        old_root = previous_thread_ts if previous_thread_ts is not None else focus
        membership_changed = old_root != thread_ts
        was_top_level = previous_thread_ts is None or (
            message_ts is not None and previous_thread_ts == message_ts
        )
        if membership_changed:
            self.invalidate_aggregates(channel_id, old_root)
            self.invalidate_aggregates(channel_id, thread_ts)
            self._upsert_thread_doc(channel_id, old_root)
            self._upsert_thread_doc(channel_id, thread_ts)
        elif text_changed:
            self.invalidate_aggregates(channel_id, thread_ts)
            self._upsert_thread_doc(channel_id, thread_ts)
        else:
            exists = self.repo._conn.execute(  # noqa: SLF001
                """
                SELECT 1 FROM search_docs
                WHERE channel_id=? AND kind='thread' AND anchor_ts=?
                """,
                (channel_id, thread_ts),
            ).fetchone()
            if not exists:
                self._upsert_thread_doc(channel_id, thread_ts)

        # Windows only contain top-level messages — skip pure reply edits.
        left_or_entered_top = membership_changed and (was_top_level or top_level_now)
        if (top_level_now and text_changed) or left_or_entered_top:
            self._invalidate_and_rebuild_windows_around(channel_id, focus)
            self.repo.mark_windows_dirty(channel_id)

    def rebuild_dirty_windows(self) -> dict[str, int]:
        """Rebuild dirty channels one-by-one; ack only the generation rebuilt."""
        pending = self.repo.peek_dirty_window_channels()
        out: dict[str, int] = {}
        for channel_id, generation in pending:
            try:
                out[channel_id] = self._rebuild_windows_for_channel(channel_id)
                self.repo.ack_dirty_window_channel(channel_id, generation)
            except Exception:
                # Leave channel (and remaining) dirty for the next pass.
                raise
        return out

    def _invalidate_and_rebuild_windows_around(
        self, channel_id: str, focus_ts: str
    ) -> None:
        """Delete windows near ``focus_ts`` and rebuild every affected bucket.

        Neighbors that shared a deleted window (e.g. endpoint removal) are
        rebuilt immediately so one-shot snapshots are not left uncovered.
        Scans only a local ts range — not the whole channel.
        """
        try:
            focus_num = float(focus_ts)
        except ValueError:
            return
        win_rows = self.repo._conn.execute(  # noqa: SLF001
            """
            SELECT doc_id, start_ts, end_ts FROM search_docs
            WHERE channel_id=? AND kind='window'
              AND CAST(start_ts AS REAL) <= ?
              AND CAST(end_ts AS REAL) >= ?
            """,
            (channel_id, focus_num + WINDOW_GAP_S, focus_num - WINDOW_GAP_S),
        ).fetchall()
        ranges: list[tuple[float, float]] = []
        for w in win_rows:
            try:
                ranges.append((float(w["start_ts"]), float(w["end_ts"])))
            except (TypeError, ValueError):
                continue
        self._delete_doc_ids([int(w["doc_id"]) for w in win_rows])
        leftover = self.repo._conn.execute(  # noqa: SLF001
            """
            SELECT doc_id, start_ts, end_ts FROM search_docs
            WHERE channel_id=? AND kind='window'
              AND CAST(start_ts AS REAL) <= ? AND CAST(end_ts AS REAL) >= ?
            """,
            (channel_id, focus_num, focus_num),
        ).fetchall()
        for w in leftover:
            try:
                ranges.append((float(w["start_ts"]), float(w["end_ts"])))
            except (TypeError, ValueError):
                continue
        self._delete_doc_ids([int(w["doc_id"]) for w in leftover])
        ranges.append((focus_num, focus_num))

        ch = self.repo._conn.execute(  # noqa: SLF001
            """
            SELECT workspace_key FROM messages
            WHERE channel_id=? AND deleted_state='none' LIMIT 1
            """,
            (channel_id,),
        ).fetchone()
        if not ch:
            return

        # Iteratively expand when new buckets overlap surviving neighbors
        # (size-splits can shift spans past the original focus neighborhood).
        for _ in range(4):
            lo = min(r[0] for r in ranges) - WINDOW_GAP_S
            hi = max(r[1] for r in ranges) + WINDOW_GAP_S
            msgs = self.repo._conn.execute(  # noqa: SLF001
                """
                SELECT * FROM messages
                WHERE channel_id=? AND deleted_state='none'
                  AND (thread_ts IS NULL OR thread_ts=ts)
                  AND ts_num >= ? AND ts_num <= ?
                ORDER BY ts_num ASC
                """,
                (channel_id, lo, hi),
            ).fetchall()
            if not msgs:
                return

            def _overlaps_affected(b0: float, b1: float) -> bool:
                return any(b0 <= a1 and b1 >= a0 for a0, a1 in ranges)

            to_flush: list[list[Any]] = []
            bucket: list[Any] = []
            for m in msgs:
                if bucket:
                    prev_ts = float(bucket[-1]["ts_num"])
                    gap = float(m["ts_num"]) - prev_ts
                    size = sum(
                        len(x["search_text"] or x["text"] or "") for x in bucket
                    )
                    if gap > WINDOW_GAP_S or size > WINDOW_MAX_CHARS:
                        b0 = float(bucket[0]["ts_num"])
                        b1 = float(bucket[-1]["ts_num"])
                        if _overlaps_affected(b0, b1):
                            to_flush.append(list(bucket))
                        bucket = []
                bucket.append(m)
            if bucket:
                b0 = float(bucket[0]["ts_num"])
                b1 = float(bucket[-1]["ts_num"])
                if _overlaps_affected(b0, b1):
                    to_flush.append(list(bucket))

            # Drop any surviving window that overlaps a bucket we are about to
            # write — otherwise size-shifted neighbors leave durable overlaps.
            expanded = False
            for nb in to_flush:
                nb0 = float(nb[0]["ts_num"])
                nb1 = float(nb[-1]["ts_num"])
                overlap = self.repo._conn.execute(  # noqa: SLF001
                    """
                    SELECT doc_id, start_ts, end_ts FROM search_docs
                    WHERE channel_id=? AND kind='window'
                      AND CAST(start_ts AS REAL) <= ? AND CAST(end_ts AS REAL) >= ?
                    """,
                    (channel_id, nb1, nb0),
                ).fetchall()
                if overlap:
                    for w in overlap:
                        try:
                            ranges.append((float(w["start_ts"]), float(w["end_ts"])))
                        except (TypeError, ValueError):
                            continue
                    self._delete_doc_ids([int(w["doc_id"]) for w in overlap])
                    expanded = True
            if expanded:
                continue
            for nb in to_flush:
                self._flush_window(ch["workspace_key"], channel_id, nb)
            break

    def _rebuild_windows_for_channel(self, channel_id: str) -> int:
        """Full window rebuild for one channel; preserve unchanged embeddings."""
        ch = self.repo._conn.execute(  # noqa: SLF001
            """
            SELECT workspace_key FROM messages
            WHERE channel_id=? AND deleted_state='none' LIMIT 1
            """,
            (channel_id,),
        ).fetchone()
        existing = self.repo._conn.execute(  # noqa: SLF001
            """
            SELECT doc_id, start_ts, end_ts, anchor_ts FROM search_docs
            WHERE channel_id=? AND kind='window'
            """,
            (channel_id,),
        ).fetchall()
        if not ch:
            self._delete_doc_ids([int(r["doc_id"]) for r in existing])
            return 0
        msgs = self.repo._conn.execute(  # noqa: SLF001
            """
            SELECT * FROM messages
            WHERE channel_id=? AND deleted_state='none'
              AND (thread_ts IS NULL OR thread_ts=ts)
            ORDER BY ts_num ASC
            """,
            (channel_id,),
        ).fetchall()
        desired_keys: set[tuple[str, str, str]] = set()
        n = 0
        bucket: list[Any] = []
        for m in msgs:
            if bucket:
                prev_ts = float(bucket[-1]["ts_num"])
                gap = float(m["ts_num"]) - prev_ts
                size = sum(len(x["search_text"] or x["text"] or "") for x in bucket)
                if gap > WINDOW_GAP_S or size > WINDOW_MAX_CHARS:
                    key = (bucket[0]["ts"], bucket[0]["ts"], bucket[-1]["ts"])
                    desired_keys.add(key)
                    self._flush_window(ch["workspace_key"], channel_id, bucket)
                    n += 1
                    bucket = []
            bucket.append(m)
        if bucket:
            key = (bucket[0]["ts"], bucket[0]["ts"], bucket[-1]["ts"])
            desired_keys.add(key)
            self._flush_window(ch["workspace_key"], channel_id, bucket)
            n += 1
        # Drop windows whose span is no longer a natural bucket (embeddings of
        # upserted-unchanged docs were preserved by upsert_search_doc).
        stale_ids = [
            int(r["doc_id"])
            for r in existing
            if (r["anchor_ts"], r["start_ts"], r["end_ts"]) not in desired_keys
        ]
        self._delete_doc_ids(stale_ids)
        return n

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
            self.upsert_message_doc(channel_id, r["ts"])  # ignore changed flag

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
