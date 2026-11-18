"""Minimal Snappy encoder that emits only literal blocks (unframed raw stream)."""

from __future__ import annotations


def _write_varint(n: int) -> bytes:
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


def compress_literals(data: bytes) -> bytes:
    """Encode `data` as a raw (unframed) Snappy stream using only literals."""
    out = bytearray()
    out.extend(_write_varint(len(data)))
    offset = 0
    while offset < len(data):
        chunk = data[offset : offset + 60]
        # tag: (len-1) << 2 | 0 for literal length 1..60
        lit_len = len(chunk)
        out.append(((lit_len - 1) << 2) | 0)
        out.extend(chunk)
        offset += lit_len
    return bytes(out)
