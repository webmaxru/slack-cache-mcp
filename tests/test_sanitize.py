"""Sanitizer property tests."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from slack_cache_mcp.desktop.sanitize import sanitize


@given(
    st.recursive(
        st.none()
        | st.booleans()
        | st.integers()
        | st.text(max_size=40)
        | st.just("xoxc-FAKESECRETVALUE111111111111111111")
        | st.just("xapp-1-FAKEAPPTOKEN000000000000000"),
        lambda children: st.lists(children, max_size=4)
        | st.dictionaries(
            st.sampled_from(
                [
                    "text",
                    "token",
                    "api_token",
                    "cookie",
                    "nested",
                    "bootData",
                    "user_id",
                    "team_id",
                    "password",
                    "session",
                ]
            ),
            children,
            max_size=6,
        ),
        max_leaves=30,
    )
)
@settings(max_examples=40)
def test_sanitize_removes_secrets(value: object) -> None:
    clean = sanitize(value)
    blob = repr(clean)
    assert "xoxc-" not in blob.lower() or "<REDACTED>" in blob
    assert "xoxc-FAKE" not in blob
    if isinstance(clean, dict):
        for k in clean:
            assert "token" not in k.lower() or k.lower() in {"team_id"}  # no *token keys
