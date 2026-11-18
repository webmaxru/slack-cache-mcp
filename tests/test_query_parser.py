from slack_cache_mcp.search.query import parse_query


def test_parse_operators() -> None:
    q = parse_query('widgets in:#general from:@alice after:2026-01-01 has:file is:thread')
    assert "widgets" in q.free_text
    assert q.filters["in"] == "general"
    assert q.filters["from"] == "alice"
    assert q.filters["after"] == "2026-01-01"
    assert "file" in q.filters["has"]
    assert "thread" in q.filters["is"]
