from slack_cache_mcp.ingest.projection import VersionCandidate, choose_current, deleted_state_for


def test_projection_prefers_non_partial_then_edit() -> None:
    versions = [
        VersionCandidate(
            channel_id="C1",
            ts="1.000000",
            content_sha256="aaa",
            edited_ts=None,
            is_partial=True,
            is_deletion=False,
            source_rank=100,
            last_seen="2026-01-02T00:00:00Z",
            text="partial",
            search_text="partial",
            user_id="U1",
            thread_ts=None,
            subtype=None,
        ),
        VersionCandidate(
            channel_id="C1",
            ts="1.000000",
            content_sha256="bbb",
            edited_ts="2.000000",
            is_partial=False,
            is_deletion=False,
            source_rank=90,
            last_seen="2026-01-01T00:00:00Z",
            text="full",
            search_text="full",
            user_id="U1",
            thread_ts=None,
            subtype=None,
        ),
    ]
    cur = choose_current(versions)
    assert cur is not None
    assert cur.content_sha256 == "bbb"
    assert deleted_state_for(versions, cur) == "none"


def test_tombstone_marks_deleted() -> None:
    versions = [
        VersionCandidate(
            channel_id="C1",
            ts="1.000000",
            content_sha256="aaa",
            edited_ts=None,
            is_partial=False,
            is_deletion=False,
            source_rank=100,
            last_seen="2026-01-01T00:00:00Z",
            text="hi",
            search_text="hi",
            user_id="U1",
            thread_ts=None,
            subtype=None,
        ),
        VersionCandidate(
            channel_id="C1",
            ts="1.000000",
            content_sha256="tomb",
            edited_ts="3.000000",
            is_partial=False,
            is_deletion=True,
            source_rank=100,
            last_seen="2026-01-02T00:00:00Z",
            text="deleted",
            search_text="deleted",
            user_id="U1",
            thread_ts=None,
            subtype="tombstone",
        ),
    ]
    cur = choose_current(versions)
    assert cur is not None
    assert deleted_state_for(versions, cur) == "deleted"


def test_edit_then_tombstone_without_edited_ts_still_deleted() -> None:
    """Tombstone without edited_ts must still mark deleted when an edit is current."""
    versions = [
        VersionCandidate(
            channel_id="C1",
            ts="1.000000",
            content_sha256="orig",
            edited_ts=None,
            is_partial=False,
            is_deletion=False,
            source_rank=50,
            last_seen="2026-01-01T00:00:00Z",
            text="orig",
            search_text="orig",
            user_id="U1",
            thread_ts=None,
            subtype=None,
        ),
        VersionCandidate(
            channel_id="C1",
            ts="1.000000",
            content_sha256="edit",
            edited_ts="2.000000",
            is_partial=False,
            is_deletion=False,
            source_rank=50,
            last_seen="2026-01-02T00:00:00Z",
            text="edited",
            search_text="edited",
            user_id="U1",
            thread_ts=None,
            subtype=None,
        ),
        VersionCandidate(
            channel_id="C1",
            ts="1.000000",
            content_sha256="tomb",
            edited_ts=None,  # fixtures often omit edited.ts on tombstones
            is_partial=False,
            is_deletion=True,
            source_rank=50,
            last_seen="2026-01-03T00:00:00Z",
            text="",
            search_text="",
            user_id="U1",
            thread_ts=None,
            subtype="tombstone",
        ),
    ]
    cur = choose_current(versions)
    assert cur is not None
    assert cur.content_sha256 == "edit"
    assert deleted_state_for(versions, cur) == "deleted"
