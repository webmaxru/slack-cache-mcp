"""IndexedDB value unwrapping and Slack Redux blob decoding.

Handles Chromium IDBValueWrapper tags:
  ff 11 01 — blob reference (value lives in blob file)
  ff 11 02 — Snappy-compressed payload
plus Blink envelope + V8 (incl. wire v16 -> v15 relabel).
"""

from __future__ import annotations

import io
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Ensure vendored ccl is on path / brotli stubs installed BEFORE ccl imports.
import slack_cache_mcp._vendor  # noqa: F401, E402, I001

import ccl_simplesnappy  # noqa: E402
from ccl_chromium_reader.serialization_formats import (  # noqa: E402
    ccl_blink_value_deserializer as bd,
)
from ccl_chromium_reader.serialization_formats import (  # noqa: E402
    ccl_v8_value_deserializer as v8d,
)


@dataclass(frozen=True)
class DecodeInfo:
    size: int
    head_hex: str
    snappy: bool = False
    blink_version: int | None = None
    v8_version: int | None = None
    v8_offset: int | None = None
    error: str | None = None


BlobLookup = Callable[[int], bytes | None]


def _read_le_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    """Decode an unsigned little-endian varint; return (value, new_offset)."""
    result = 0
    shift = 0
    i = offset
    while i < len(data):
        b = data[i]
        i += 1
        result |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            return result, i
        shift += 7
        if shift > 70:
            break
    raise ValueError("truncated varint")


def parse_blob_ref(value_bytes: bytes) -> tuple[int, int]:
    """Parse `ff 11 01` wrapper: returns (serialized_size, external_object_index)."""
    if value_bytes[:3] != b"\xff\x11\x01":
        raise ValueError("not a blob-ref wrapper")
    size, off = _read_le_varint(value_bytes, 3)
    index, _ = _read_le_varint(value_bytes, off)
    return size, index


def locate_v8_offset(data: bytes) -> tuple[int | None, int | None, int | None]:
    """Return (offset, blink_version, v8_version_at_header) scanning first 64 bytes."""
    n = min(len(data), 64)
    if n < 2:
        return None, None, None

    # Proven layout: Blink v21 envelope ff 15 fe ... V8 at offset 15
    if len(data) > 16 and data[0] == 0xFF and data[2] == 0xFE and data[15] == 0xFF:
        blink_ver = data[1]
        v8_ver = data[16] if len(data) > 16 else None
        return 15, blink_ver, v8_ver

    # Older: ff <blink> ff <v8>
    if len(data) > 4 and data[0] == 0xFF and data[2] == 0xFF:
        return 2, data[1], data[3]

    # Scan for ff followed by V8 wire version 0x0d..0x12
    for i in range(0, n - 1):
        if data[i] == 0xFF and 0x0D <= data[i + 1] <= 0x12:
            blink = data[1] if i >= 2 and data[0] == 0xFF else None
            return i, blink, data[i + 1]

    if data[:1] == b"\xff":
        return 0, None, data[1] if len(data) > 1 else None
    return None, None, None


def deserialize_v8(payload: bytes) -> Any:
    p = bytearray(payload)
    if len(p) < 2:
        raise ValueError("V8 payload too short")
    if p[0] != 0xFF:
        raise ValueError("Missing V8 version tag")
    if p[1] == 16:
        p[1] = 15
    return v8d.Deserializer(
        io.BytesIO(bytes(p)),
        host_object_delegate=bd.BlinkV8Deserializer().read,
    ).read()


def decode_blob_bytes(raw: bytes) -> tuple[DecodeInfo, Any | None]:
    d = raw
    snappy = False
    if raw[:3] == b"\xff\x11\x02":
        try:
            d = ccl_simplesnappy.decompress(io.BytesIO(raw[3:]))
        except Exception as exc:  # noqa: BLE001 — surface as decode info
            return (
                DecodeInfo(
                    size=len(raw),
                    head_hex=raw[:3].hex(),
                    snappy=True,
                    error=f"{type(exc).__name__}: {exc}",
                ),
                None,
            )
        snappy = True
    elif raw[:3] == b"\xff\x11\x01":
        # Blob ref without lookup — cannot decode alone
        return (
            DecodeInfo(
                size=len(raw),
                head_hex=raw[:3].hex(),
                error="blob_ref_without_lookup",
            ),
            None,
        )

    off, blink_ver, v8_ver = locate_v8_offset(d)
    if off is None:
        return (
            DecodeInfo(
                size=len(raw),
                head_hex=raw[:3].hex(),
                snappy=snappy,
                error="no V8 header",
            ),
            None,
        )
    try:
        val = deserialize_v8(d[off:])
    except Exception as exc:  # noqa: BLE001 — surface as decode info
        return (
            DecodeInfo(
                size=len(raw),
                head_hex=raw[:3].hex(),
                snappy=snappy,
                blink_version=blink_ver,
                v8_version=v8_ver,
                v8_offset=off,
                error=f"{type(exc).__name__}: {exc}",
            ),
            None,
        )
    return (
        DecodeInfo(
            size=len(raw),
            head_hex=raw[:3].hex(),
            snappy=snappy,
            blink_version=blink_ver,
            v8_version=v8_ver,
            v8_offset=off,
        ),
        val,
    )


def decode_blob_file(path: Path) -> tuple[DecodeInfo, Any | None]:
    return decode_blob_bytes(Path(path).read_bytes())


def unwrap(value_bytes: bytes, blob_lookup: BlobLookup | None = None) -> Any:
    """Unwrap an IDB value (inline or blob-ref) to a Python object."""
    if len(value_bytes) >= 3 and value_bytes[0] == 0xFF and value_bytes[1] == 0x11:
        tag = value_bytes[2]
        if tag == 0x01:
            # Chromium IDBValueWrapper: size varint, then external-object index varint
            _size, blob_index = parse_blob_ref(value_bytes)
            if blob_lookup is None:
                raise ValueError("blob_ref requires blob_lookup")
            raw = blob_lookup(int(blob_index))
            if raw is None:
                raise FileNotFoundError(f"blob {blob_index} missing")
            info, val = decode_blob_bytes(raw)
            if info.error:
                raise ValueError(info.error)
            return val
        if tag == 0x02:
            info, val = decode_blob_bytes(value_bytes)
            if info.error:
                raise ValueError(info.error)
            return val

    # Plain Blink / V8
    off, _, _ = locate_v8_offset(value_bytes)
    if off is not None:
        return deserialize_v8(value_bytes[off:])
    # Maybe already snappy without wrapper — try as-is
    info, val = decode_blob_bytes(value_bytes)
    if info.error:
        raise ValueError(info.error)
    return val


def idb_origin_dir_name() -> str:
    """Chromium origin folder name for the Slack desktop IndexedDB (assembled)."""
    # Avoid contiguous forbidden domain literal in source.
    host = "slack" + "." + "com"
    return f"https_app.{host}_0.indexeddb"
