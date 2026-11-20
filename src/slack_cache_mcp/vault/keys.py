"""Vault key material: keyring (DPAPI/Credential Manager) with file fallback for tests."""

from __future__ import annotations

import contextlib
import os
import secrets
from pathlib import Path

_SERVICE = "slack-cache-mcp"
_USERNAME = "vault-aes256"


class VaultKeyError(RuntimeError):
    """Raised when vault key material cannot be loaded or created."""


def _file_key_path(data_dir: Path) -> Path:
    return data_dir / "vault.key"


def _read_file_key(path: Path) -> bytes | None:
    if not path.is_file():
        return None
    raw = path.read_bytes().strip()
    if len(raw) == 32:
        return raw
    # hex encoding
    try:
        decoded = bytes.fromhex(raw.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None
    return decoded if len(decoded) == 32 else None


def _write_file_key(path: Path, key: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Store as hex for safer text tooling; still 256-bit entropy.
    path.write_text(key.hex() + "\n", encoding="ascii")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def load_or_create_vault_key(data_dir: Path, *, prefer_keyring: bool = True) -> bytes:
    """Return a 32-byte AES key, creating one if absent.

    Preference order:
    1. Existing keyring secret (when prefer_keyring)
    2. Existing ``vault.key`` under data_dir
    3. Create new key: try keyring first, always also write file fallback for recovery
    """
    data_dir = Path(data_dir)
    file_path = _file_key_path(data_dir)

    if prefer_keyring:
        try:
            import keyring

            stored = keyring.get_password(_SERVICE, _USERNAME)
            if stored:
                key = bytes.fromhex(stored)
                if len(key) == 32:
                    return key
        except Exception:
            pass

    existing = _read_file_key(file_path)
    if existing is not None:
        return existing

    key = secrets.token_bytes(32)
    if prefer_keyring:
        try:
            import keyring

            keyring.set_password(_SERVICE, _USERNAME, key.hex())
        except Exception:
            pass
    _write_file_key(file_path, key)
    return key
