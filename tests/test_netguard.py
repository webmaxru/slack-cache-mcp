"""Runtime audit hook blocks non-loopback network use."""

from __future__ import annotations

import socket

import pytest

from slack_cache_mcp import netguard


def test_guard_blocks_non_loopback_connect() -> None:
    netguard.install()
    with pytest.raises(netguard.NetworkGuardError):
        socket.create_connection(("example.com", 443), timeout=1)


def test_guard_blocks_getaddrinfo_for_external() -> None:
    netguard.install()
    with pytest.raises(netguard.NetworkGuardError):
        socket.getaddrinfo("example.com", 443)


def test_guard_allows_loopback_getaddrinfo() -> None:
    netguard.install()
    infos = socket.getaddrinfo("127.0.0.1", 9, type=socket.SOCK_STREAM)
    assert infos


def test_guard_blocks_http_client_connect() -> None:
    netguard.install()
    # Note: CPython 3.12 on this host does not emit a urllib.Request audit event
    # when constructing Request; http.client.connect is audited and blocked.
    import http.client

    with pytest.raises(netguard.NetworkGuardError):
        http.client.HTTPSConnection("example.com").connect()
