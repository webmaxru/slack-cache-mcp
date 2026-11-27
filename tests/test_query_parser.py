from slack_cache_mcp.search.query import (
    parse_query,
    parse_time_bound,
    unsupported_operators,
)


def test_parse_operators() -> None:
    q = parse_query('widgets in:#general from:@alice after:2026-01-01 has:file is:thread')
    assert "widgets" in q.free_text
    assert q.filters["in"] == "general"
    assert q.filters["from"] == "alice"
    assert q.filters["after"] == "2026-01-01"
    assert "file" in q.filters["has"]
    assert "thread" in q.filters["is"]


def test_parse_keeps_at_for_dm_in() -> None:
    q = parse_query("in:@bob hello")
    assert q.filters["in"] == "@bob"
    assert q.free_text == "hello"


def test_parse_time_bound_dates() -> None:
    start = parse_time_bound("2026-01-01", end=False)
    end = parse_time_bound("2026-01-01", end=True)
    assert end - start == 86400.0
    assert parse_time_bound("1700000000.0") == 1700000000.0
    # Explicit Z must stay UTC (not reinterpreted as Europe/Paris).
    assert parse_time_bound("2026-11-01T00:00:00Z") == 1793491200.0


def test_unsupported_ops() -> None:
    q = parse_query("widgets has:emoji is:channel")
    assert "has:emoji" in unsupported_operators(q)
    assert "is:channel" in unsupported_operators(q)


def test_during_and_with_parsed() -> None:
    q = parse_query("widgets with:@bob during:2026-11")
    assert q.filters["with"] == "bob"
    assert q.filters["during"] == "2026-11"
    assert unsupported_operators(q) == []


def test_operator_only_leaves_empty_free_text() -> None:
    q = parse_query("in:#general from:@alice")
    assert q.free_text == ""
