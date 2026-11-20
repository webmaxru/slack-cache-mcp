"""Deterministic Slack ts ordering keys."""

from __future__ import annotations

from slack_cache_mcp.ingest.coverage import merge_coverage_interval, ts_in_window, ts_key


def test_ts_key_avoids_float_ambiguity() -> None:
    # Values that can collide or reorder under float64 for large epochs.
    a = "1763400000.000100"
    b = "1763400000.000200"
    assert ts_key(a) < ts_key(b)
    assert ts_key("100.000001") == (100, 1)
    assert ts_key("bad") == (0, 0)


def test_ts_in_window_and_merge() -> None:
    assert ts_in_window("2.0", "1.0", "3.0")
    assert not ts_in_window("4.0", "1.0", "3.0")
    merged = merge_coverage_interval([("1.0", "2.0")], "2.0", "4.0")
    assert merged == [("1.0", "4.0")]
