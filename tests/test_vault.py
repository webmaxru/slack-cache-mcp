"""Vault encrypt/decrypt + no-xox + retention."""

from __future__ import annotations

from pathlib import Path

import pytest

from slack_cache_mcp.vault import store as vault_store
from slack_cache_mcp.vault.store import VaultStore


def test_vault_roundtrip_and_no_xox(tmp_path: Path) -> None:
    key = b"\x01" * 32
    vault = VaultStore(tmp_path / "vault", key=key, keep=True, keep_days=30)
    state = {
        "bootData": {"team_id": "T1", "user_id": "U1", "api_token": "xoxc-FAKE000"},
        "messages": {"C1": {"1.0": {"ts": "1.0", "text": "hi", "user": "U1"}}},
    }
    res = vault.write_state(state)
    assert res is not None
    assert res.created
    assert vault.has(res.state_sha256)
    loaded = vault.read_state(res.state_sha256)
    assert "api_token" not in str(loaded)
    assert vault.scan_for_token_leak() == []
    # idempotent second write
    res2 = vault.write_state(state, state_sha256=res.state_sha256)
    assert res2 is not None
    assert res2.created is False


def test_vault_disabled(tmp_path: Path) -> None:
    vault = VaultStore(tmp_path / "vault", key=b"\x02" * 32, keep=False)
    assert vault.write_state({"a": 1}) is None


def test_vault_decompress_respects_max_output_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vault_store, "_MAX_PLAINTEXT_BYTES", 64)
    vault = VaultStore(tmp_path / "vault", key=b"\x03" * 32, keep=True)
    res = vault.write_state({"pad": "x" * 200})
    assert res is not None
    with pytest.raises(ValueError, match="decompress"):
        vault.read_state(res.state_sha256)
