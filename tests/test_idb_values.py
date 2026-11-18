"""Unit tests for unwrap / blob decode branches."""

from __future__ import annotations

import pytest

from slack_cache_mcp.desktop.chromium.idb_values import (
    decode_blob_bytes,
    locate_v8_offset,
    parse_blob_ref,
    unwrap,
)
from tests.fixture_gen.v8_serialize import V8Serializer, wrap_blink_v21, wrap_idb_snappy


def _encode_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | 0x80 if n else b)
        if not n:
            break
    return bytes(out)


def _blob_ref(size: int, index: int) -> bytes:
    return b"\xff\x11\x01" + _encode_varint(size) + _encode_varint(index)


def test_locate_v8_offset_blink_v21() -> None:
    v8 = V8Serializer().dumps({"a": 1})
    blink = wrap_blink_v21(v8)
    off, blink_ver, v8_ver = locate_v8_offset(blink)
    assert off == 15
    assert blink_ver == 0x15


def test_decode_snappy_blob_roundtrip() -> None:
    payload = {"messages": {"C1": {}}, "channels": {}, "bootData": {"team_id": "T1"}}
    v8 = bytearray(V8Serializer().dumps(payload))
    v8[1] = 16
    blob = wrap_idb_snappy(wrap_blink_v21(bytes(v8)))
    info, val = decode_blob_bytes(blob)
    assert info.error is None
    assert info.snappy is True
    assert isinstance(val, dict)
    assert val["bootData"]["team_id"] == "T1"


def test_unwrap_plain_and_snappy() -> None:
    inner = {"ok": True}
    v8 = V8Serializer().dumps(inner)
    blink = wrap_blink_v21(v8)
    assert unwrap(blink)["ok"] is True
    assert unwrap(wrap_idb_snappy(blink))["ok"] is True


def test_parse_blob_ref_varints() -> None:
    # ff 11 01 80 01 07 → size 128, index 7 (not little-endian uint64)
    raw = bytes.fromhex("ff1101800107")
    size, index = parse_blob_ref(raw)
    assert size == 128
    assert index == 7


def test_unwrap_blob_ref() -> None:
    inner = {"x": 2}
    blob_bytes = wrap_idb_snappy(wrap_blink_v21(V8Serializer().dumps(inner)))
    ref = _blob_ref(size=len(blob_bytes), index=7)

    def lookup(n: int) -> bytes | None:
        return blob_bytes if n == 7 else None

    assert unwrap(ref, lookup)["x"] == 2


def test_unwrap_blob_ref_missing() -> None:
    ref = _blob_ref(size=1, index=1)
    with pytest.raises(FileNotFoundError):
        unwrap(ref, lambda n: None)
