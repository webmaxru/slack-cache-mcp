"""Minimal V8 value serializer for JSON-like values (wire version 15)."""

from __future__ import annotations

import struct
from typing import Any


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _zigzag(n: int) -> int:
    return (n << 1) ^ (n >> 31)


class V8Serializer:
    def __init__(self, version: int = 15) -> None:
        self.version = version
        self._buf = bytearray()
        self._objects: list[Any] = []

    def dumps(self, value: Any) -> bytes:
        self._buf = bytearray()
        self._objects = []
        self._buf.append(0xFF)
        self._buf.append(self.version & 0xFF)
        self._write(value)
        return bytes(self._buf)

    def _write(self, value: Any) -> None:
        if value is None:
            self._buf.extend(b"0")
            return
        if isinstance(value, bool):
            self._buf.extend(b"T" if value else b"F")
            return
        if isinstance(value, int) and not isinstance(value, bool):
            if -0x80000000 <= value <= 0x7FFFFFFF:
                self._buf.extend(b"I")
                self._buf.extend(_varint(_zigzag(value)))
            else:
                self._buf.extend(b"N")
                self._buf.extend(struct.pack("<d", float(value)))
            return
        if isinstance(value, float):
            self._buf.extend(b"N")
            self._buf.extend(struct.pack("<d", value))
            return
        if isinstance(value, str):
            # Prefer one-byte string when all code points are ASCII
            try:
                ascii_bytes = value.encode("ascii")
                self._buf.extend(b'"')
                self._buf.extend(_varint(len(ascii_bytes)))
                self._buf.extend(ascii_bytes)
            except UnicodeEncodeError:
                enc = value.encode("utf-16-le")
                self._buf.extend(b"c")
                self._buf.extend(_varint(len(enc)))
                self._buf.extend(enc)
            return
        if isinstance(value, list):
            self._objects.append(value)
            self._buf.extend(b"A")
            self._buf.extend(_varint(len(value)))
            for item in value:
                self._write(item)
            self._buf.extend(b"$")  # EndDenseJSArray
            self._buf.extend(_varint(0))  # property count
            self._buf.extend(_varint(len(value)))  # length
            return
        if isinstance(value, dict):
            self._objects.append(value)
            self._buf.extend(b"o")
            for k, v in value.items():
                self._write(str(k))
                self._write(v)
            self._buf.extend(b"{")
            self._buf.extend(_varint(len(value)))
            return
        raise TypeError(f"Unsupported type for V8 serialize: {type(value)!r}")


def wrap_blink_v21(v8_payload: bytes) -> bytes:
    """Wrap a V8 payload in a Blink SSV v21 envelope (ff 15 fe ...; V8 at offset 15)."""
    # Layout matching the probe: d[0]=ff, d[1]=0x15, d[2]=fe, then 12 trailer bytes, V8 at 15.
    header = bytearray(15)
    header[0] = 0xFF
    header[1] = 0x15  # Blink version 21
    header[2] = 0xFE
    # remaining header bytes left zero (enough for ccl/probe offset-15 locate)
    return bytes(header) + v8_payload


def wrap_idb_snappy(blink_or_v8: bytes) -> bytes:
    """Prefix ff 11 02 and Snappy-compress (literal encoder)."""
    from tests.fixture_gen.snappy_literal import compress_literals

    return b"\xff\x11\x02" + compress_literals(blink_or_v8)
