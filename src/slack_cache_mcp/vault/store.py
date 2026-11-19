"""Encrypted, content-addressed vault objects (zstd + AES-256-GCM)."""

from __future__ import annotations

import io
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import zstandard as zstd
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from slack_cache_mcp.desktop.sanitize import sanitize
from slack_cache_mcp.vault.keys import load_or_create_vault_key

_MAGIC = b"SCV1"  # slackcache vault v1
_NONCE_LEN = 12
# Bound decompressed plaintext to blunt crafted vault decompression bombs.
# Real Redux states are multi-MB; 256 MiB leaves headroom without unbounded RAM.
_MAX_PLAINTEXT_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class VaultWriteResult:
    state_sha256: str
    relative_path: str
    bytes_written: int
    created: bool


def _utc_now() -> datetime:
    return datetime.now(UTC)


def vault_object_relpath(state_sha256: str) -> str:
    """Two-level fan-out: ab/abcdef....zst.enc"""
    return f"{state_sha256[:2]}/{state_sha256}.zst.enc"


def _decompress_bounded(compressed: bytes, *, max_bytes: int) -> bytes:
    """Decompress zstd with an explicit plaintext size cap (anti bomb)."""
    try:
        params = zstd.get_frame_parameters(compressed)
        declared = int(params.content_size)

    except zstd.ZstdError as exc:
        raise ValueError("invalid vault zstd frame") from exc
    # CONTENTSIZE_UNKNOWN is -1 in zstandard bindings.
    if declared >= 0 and declared > max_bytes:
        raise ValueError(f"vault decompress failed or exceeded {max_bytes} byte cap")
    if declared >= 0:
        try:
            return zstd.ZstdDecompressor().decompress(compressed)
        except zstd.ZstdError as exc:
            raise ValueError("vault decompress failed") from exc
    # Unknown content size: stream and enforce the cap incrementally.
    reader = zstd.ZstdDecompressor().stream_reader(io.BytesIO(compressed))
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = reader.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"vault decompress failed or exceeded {max_bytes} byte cap")
        chunks.append(chunk)
    return b"".join(chunks)


def canonical_sanitized_json(state: Any) -> tuple[bytes, str]:
    """Sanitize -> canonical JSON bytes + sha256 hex (caller may already have sha)."""
    import hashlib

    clean = sanitize(state)
    blob = json.dumps(clean, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    raw = blob.encode("utf-8")
    return raw, hashlib.sha256(raw).hexdigest()


class VaultStore:
    def __init__(
        self,
        vault_dir: Path,
        *,
        key: bytes | None = None,
        data_dir: Path | None = None,
        keep: bool = True,
        keep_days: int = 30,
    ) -> None:
        self.vault_dir = Path(vault_dir)
        self.keep = keep
        self.keep_days = keep_days
        if key is not None:
            if len(key) != 32:
                raise ValueError("vault key must be 32 bytes")
            self._key = key
        else:
            root = Path(data_dir) if data_dir is not None else self.vault_dir.parent
            self._key = load_or_create_vault_key(root)
        self.vault_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, state_sha256: str) -> Path:
        return self.vault_dir / vault_object_relpath(state_sha256)

    def has(self, state_sha256: str) -> bool:
        return self.path_for(state_sha256).is_file()

    def write_state(
        self,
        state: Any,
        *,
        state_sha256: str | None = None,
    ) -> VaultWriteResult | None:
        """Write sanitized state if keep=true. Returns None when vault disabled."""
        if not self.keep:
            return None
        raw, computed = canonical_sanitized_json(state)
        sha = state_sha256 or computed
        if state_sha256 is not None and state_sha256 != computed:
            # Prefer the caller's hash only when it matches canonical form.
            sha = computed
        dest = self.path_for(sha)
        if dest.is_file():
            # Refresh mtime for retention
            os.utime(dest, None)
            return VaultWriteResult(sha, vault_object_relpath(sha), dest.stat().st_size, False)

        compressed = zstd.ZstdCompressor(level=3).compress(raw)
        nonce = os.urandom(_NONCE_LEN)
        aead = AESGCM(self._key)
        # AAD binds ciphertext to content hash
        ct = aead.encrypt(nonce, compressed, sha.encode("ascii"))
        payload = _MAGIC + nonce + ct
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(payload)
        tmp.replace(dest)
        return VaultWriteResult(sha, vault_object_relpath(sha), len(payload), True)

    def read_state(self, state_sha256: str) -> Any:
        path = self.path_for(state_sha256)
        data = path.read_bytes()
        if not data.startswith(_MAGIC):
            raise ValueError("invalid vault magic")
        body = data[len(_MAGIC) :]
        nonce, ct = body[:_NONCE_LEN], body[_NONCE_LEN:]
        aead = AESGCM(self._key)
        compressed = aead.decrypt(nonce, ct, state_sha256.encode("ascii"))
        raw = _decompress_bounded(compressed, max_bytes=_MAX_PLAINTEXT_BYTES)
        return json.loads(raw.decode("utf-8"))

    def purge_expired(self) -> int:
        """Delete vault objects older than keep_days. Returns count removed."""
        if self.keep_days <= 0:
            return 0
        cutoff = _utc_now() - timedelta(days=self.keep_days)
        removed = 0
        if not self.vault_dir.is_dir():
            return 0
        for path in self.vault_dir.rglob("*.zst.enc"):
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            except OSError:
                continue
            if mtime < cutoff:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    def scan_for_token_leak(self) -> list[str]:
        """Decrypt all objects and search for xox* (test helper)."""
        import re

        xox = re.compile(rb"xox[a-z]-", re.I)
        offenders: list[str] = []
        if not self.vault_dir.is_dir():
            return offenders
        for path in self.vault_dir.rglob("*.zst.enc"):
            sha = path.stem.replace(".zst", "")
            # filename is <sha>.zst.enc → stem is <sha>.zst
            name = path.name
            if name.endswith(".zst.enc"):
                sha = name[: -len(".zst.enc")]
            try:
                obj = self.read_state(sha)
            except Exception:
                continue
            blob = json.dumps(obj).encode("utf-8", "replace")
            if xox.search(blob):
                offenders.append(sha)
        return offenders
