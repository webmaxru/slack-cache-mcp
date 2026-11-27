"""Local device identity: ULID + Ed25519 (sign) + age X25519 (encrypt).

Private keys prefer OS keyring (Windows Credential Manager / DPAPI) with a
file fallback under the data dir for tests and recovery — same pattern as vault.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ulid
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pyrage import x25519

_SERVICE = "slack-cache-mcp"


@dataclass(frozen=True)
class DevicePublicInfo:
    device_id: str
    label: str
    ed25519_public_b64: str
    age_recipient: str

    def to_dict(self) -> dict[str, str]:
        return {
            "device_id": self.device_id,
            "label": self.label,
            "ed25519_public": self.ed25519_public_b64,
            "age_recipient": self.age_recipient,
        }


@dataclass
class DeviceIdentity:
    device_id: str
    label: str
    ed25519_private: Ed25519PrivateKey
    age_identity: x25519.Identity

    @property
    def ed25519_public_b64(self) -> str:
        pub = self.ed25519_private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return base64.b64encode(pub).decode("ascii")

    @property
    def age_recipient(self) -> str:
        return str(self.age_identity.to_public())

    def public_info(self) -> DevicePublicInfo:
        return DevicePublicInfo(
            device_id=self.device_id,
            label=self.label,
            ed25519_public_b64=self.ed25519_public_b64,
            age_recipient=self.age_recipient,
        )

    def sign(self, payload: bytes) -> bytes:
        return self.ed25519_private.sign(payload)

    def public_key_json(self) -> str:
        """Stored in ``devices.public_key`` (JSON of both public halves)."""
        return json.dumps(
            {
                "ed25519": self.ed25519_public_b64,
                "age": self.age_recipient,
            },
            sort_keys=True,
        )


def _device_json_path(data_dir: Path) -> Path:
    return Path(data_dir) / "device.json"


def _secrets_path(data_dir: Path) -> Path:
    return Path(data_dir) / "device.secrets"


def _keyring_user(kind: str, device_id: str) -> str:
    return f"device-{kind}-{device_id}"


def _load_keyring_secret(kind: str, device_id: str) -> str | None:
    try:
        import keyring

        return keyring.get_password(_SERVICE, _keyring_user(kind, device_id))
    except Exception:
        return None


def _store_keyring_secret(kind: str, device_id: str, value: str) -> None:
    with contextlib.suppress(Exception):
        import keyring

        keyring.set_password(_SERVICE, _keyring_user(kind, device_id), value)


def _ed25519_private_from_b64(raw_b64: str) -> Ed25519PrivateKey:
    raw = base64.b64decode(raw_b64.encode("ascii"))
    return Ed25519PrivateKey.from_private_bytes(raw)


def _ed25519_private_to_b64(key: Ed25519PrivateKey) -> str:
    raw = key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return base64.b64encode(raw).decode("ascii")


def _read_secrets_file(path: Path) -> dict[str, str] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    ed = data.get("ed25519_private")
    age = data.get("age_identity")
    if isinstance(ed, str) and isinstance(age, str):
        return {"ed25519_private": ed, "age_identity": age}
    return None


def _write_secrets_file(path: Path, ed_b64: str, age_secret: str) -> None:
    """Write private keys with restrictive mode from creation (POSIX 0600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            {"ed25519_private": ed_b64, "age_identity": age_secret},
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    # Create/truncate with 0600 so a permissive umask cannot expose the file
    # between write_text and chmod. On Windows the mode is advisory.
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        os.write(fd, payload)
        fchmod = getattr(os, "fchmod", None)
        if callable(fchmod):
            with contextlib.suppress(OSError):
                fchmod(fd, 0o600)
    finally:
        os.close(fd)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def load_or_create_device(
    data_dir: Path,
    *,
    label: str = "local",
    prefer_keyring: bool = True,
) -> DeviceIdentity:
    """Load or create the local device identity under ``data_dir``."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    meta_path = _device_json_path(data_dir)
    secrets_path = _secrets_path(data_dir)

    device_id: str | None = None
    stored_label = label
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            device_id = str(meta["device_id"])
            stored_label = str(meta.get("label") or label)
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            device_id = None

    ed_b64: str | None = None
    age_secret: str | None = None

    if device_id and prefer_keyring:
        ed_b64 = _load_keyring_secret("ed25519", device_id)
        age_secret = _load_keyring_secret("age", device_id)

    if device_id and (not ed_b64 or not age_secret):
        file_secrets = _read_secrets_file(secrets_path)
        if file_secrets:
            ed_b64 = ed_b64 or file_secrets["ed25519_private"]
            age_secret = age_secret or file_secrets["age_identity"]

    if device_id and ed_b64 and age_secret:
        return DeviceIdentity(
            device_id=device_id,
            label=stored_label,
            ed25519_private=_ed25519_private_from_b64(ed_b64),
            age_identity=x25519.Identity.from_str(age_secret),
        )

    # Existing public identity without recoverable private keys: fail closed.
    # Silently minting new keys under the same device_id would invalidate peers
    # and make prior bundles unreadable.
    if device_id is not None:
        raise RuntimeError(
            f"device {device_id} is registered in device.json but private keys "
            "are missing from keyring and device.secrets; refusing to rotate identity"
        )

    # Create fresh identity.
    device_id = str(ulid.new())
    ed_key = Ed25519PrivateKey.generate()
    age_id = x25519.Identity.generate()
    ed_b64 = _ed25519_private_to_b64(ed_key)
    age_secret = str(age_id)

    keyring_ok = False
    if prefer_keyring:
        _store_keyring_secret("ed25519", device_id, ed_b64)
        _store_keyring_secret("age", device_id, age_secret)
        keyring_ok = (
            _load_keyring_secret("ed25519", device_id) == ed_b64
            and _load_keyring_secret("age", device_id) == age_secret
        )
    # File is a fallback only when keyring did not confirm storage (tests / recovery).
    if not keyring_ok:
        _write_secrets_file(secrets_path, ed_b64, age_secret)

    identity = DeviceIdentity(
        device_id=device_id,
        label=stored_label,
        ed25519_private=ed_key,
        age_identity=age_id,
    )
    meta_path.write_text(
        json.dumps(
            {
                "device_id": device_id,
                "label": stored_label,
                "ed25519_public": identity.ed25519_public_b64,
                "age_recipient": identity.age_recipient,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return identity


def verify_ed25519(public_b64: str, payload: bytes, signature: bytes) -> bool:
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64.encode("ascii")))
        pub.verify(signature, payload)
        return True
    except Exception:
        return False


def parse_public_key_json(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    if isinstance(data.get("ed25519"), str):
        out["ed25519"] = data["ed25519"]
    if isinstance(data.get("age"), str):
        out["age"] = data["age"]
    return out


def peer_from_dict(data: dict[str, Any]) -> DevicePublicInfo:
    return DevicePublicInfo(
        device_id=str(data["device_id"]),
        label=str(data.get("label") or data["device_id"]),
        ed25519_public_b64=str(data["ed25519_public"]),
        age_recipient=str(data["age_recipient"]),
    )
