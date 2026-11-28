"""OneDrive exchange-folder helpers for M6.0 living sync.

Filesystem-only: no Microsoft Graph / OneDrive APIs. The exchange directory
holds write-once ``bundle-*.jsonl.zst.age`` ciphertext. Never put ``archive.db``
or shared cursors here.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import sys
from pathlib import Path
from typing import Any

# Locked default for Maxim's Windows Business OneDrive root.
_DEFAULT_SEGMENTS = ("OneDrive - Microsoft", "Projects", "slackcache")

# Env override for CI / USB / Personal OneDrive / tests (absolute path).
EXCHANGE_DIR_ENV = "SLACKCACHE_EXCHANGE_DIR"

BUNDLE_GLOB = "bundle-*.jsonl.zst.age"

# Windows Files On-Demand attributes (winnt.h).
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
_FILE_ATTRIBUTE_UNPINNED = 0x00100000  # retention policy — not hydration
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF

_PIN_HINT = (
    "Right-click the exchange folder → Always keep on this device "
    "(Files On-Demand). Online-only placeholders cannot be read offline."
)

_README_TEXT = """slack-cache-mcp OneDrive exchange (M6.0)

Put ONLY immutable age bundles here (bundle-*.jsonl.zst.age).
Do NOT put archive.db, device.secrets, vault/, or shared cursor files here.

Pin this folder: Always keep on this device.
Local archive stays under %LOCALAPPDATA%\\slackcache (outside OneDrive).

Commands:
  uv run slackcache sync push
  uv run slackcache sync pull
  uv run slackcache sync status
"""


def user_profile() -> Path:
    env = os.environ.get("USERPROFILE")
    if env:
        return Path(env)
    return Path.home()


def default_onedrive_root(*, userprofile: Path | None = None) -> Path:
    root = Path(userprofile) if userprofile is not None else user_profile()
    return root / _DEFAULT_SEGMENTS[0]


def _env_exchange_override() -> Path | None:
    """Return a non-empty ``SLACKCACHE_EXCHANGE_DIR`` override, else None."""
    raw = os.environ.get(EXCHANGE_DIR_ENV)
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    return Path(stripped).expanduser()


def default_exchange_dir(*, userprofile: Path | None = None) -> Path:
    """Resolve the default OneDrive exchange path (does not create it).

    Order:
    1. non-empty ``SLACKCACHE_EXCHANGE_DIR`` (absolute or expanduser).
    2. ``%USERPROFILE%\\OneDrive - Microsoft\\Projects\\slackcache``.
    """
    override = _env_exchange_override()
    if override is not None:
        return override
    root = Path(userprofile) if userprofile is not None else user_profile()
    return root.joinpath(*_DEFAULT_SEGMENTS)


def exchange_peer_key(exchange_dir: Path, peer: str = "folder") -> str:
    """Scope the default ``folder`` push/pull cursor to the exchange path.

    Explicit ``--peer`` values are preserved unchanged so operators can share a
    watermark across destinations when they intend to.
    """
    if peer != "folder":
        return peer
    try:
        resolved = str(Path(exchange_dir).resolve())
    except OSError:
        resolved = str(exchange_dir)
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]
    return f"folder:{digest}"


def ensure_exchange_dir(
    path: Path | None = None,
    *,
    create: bool = True,
    write_readme: bool = True,
) -> Path:
    """Return the exchange directory, creating it (and a short README) if needed.

    Implicit default-path creation requires the built-in OneDrive root to exist
    so we never invent a non-synced ``OneDrive - Microsoft`` tree. Explicit
    ``path`` arguments and ``SLACKCACHE_EXCHANGE_DIR`` may create freely.
    """
    explicit = path is not None
    env_override = _env_exchange_override() is not None
    # Narrow via `path is not None` (not a separate bool) so mypy accepts Path(...).
    target = Path(path) if path is not None else default_exchange_dir()
    if create:
        if not explicit and not env_override:
            root = default_onedrive_root()
            if not root.is_dir():
                raise FileNotFoundError(
                    f"OneDrive root not found: {root}. "
                    f"Sign in to OneDrive, or set {EXCHANGE_DIR_ENV} / pass an explicit path."
                )
        target.mkdir(parents=True, exist_ok=True)
        if write_readme:
            readme = target / "README.txt"
            if not readme.exists():
                with contextlib.suppress(OSError):
                    readme.write_text(_README_TEXT, encoding="utf-8")
    return target


def file_attributes(path: Path) -> int | None:
    """Return Win32 file attributes, or None when unavailable."""
    if sys.platform != "win32":
        return None
    try:
        # Prefer pathlib/os.stat when Python exposes st_file_attributes.
        st = path.stat(follow_symlinks=False)
        attrs = getattr(st, "st_file_attributes", None)
        if isinstance(attrs, int):
            return attrs
    except OSError:
        return None
    try:
        import ctypes

        GetFileAttributesW = ctypes.windll.kernel32.GetFileAttributesW
        GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
        GetFileAttributesW.restype = ctypes.c_uint32
        raw = GetFileAttributesW(str(path))
        if raw == _INVALID_FILE_ATTRIBUTES:
            return None
        return int(raw)
    except Exception:
        return None


def is_cloud_placeholder(path: Path) -> bool | None:
    """True if the path looks like an online-only OneDrive placeholder.

    Returns None when the platform cannot tell (non-Windows / attribute missing).
    ``FILE_ATTRIBUTE_UNPINNED`` is ignored — it is a retention hint, not hydration.
    """
    attrs = file_attributes(path)
    if attrs is None:
        return None
    mask = _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | _FILE_ATTRIBUTE_RECALL_ON_OPEN
    return bool(attrs & mask)


def list_bundle_paths(exchange_dir: Path) -> list[Path]:
    if not exchange_dir.is_dir():
        return []
    return sorted(exchange_dir.glob(BUNDLE_GLOB))


def exchange_status(
    exchange_dir: Path | None = None,
    *,
    create: bool = False,
) -> dict[str, Any]:
    """Counts-only status for the exchange folder (no message text, no network)."""
    path = Path(exchange_dir) if exchange_dir is not None else default_exchange_dir()
    if create:
        ensure_exchange_dir(
            exchange_dir if exchange_dir is not None else None,
            create=True,
        )

    exists = path.is_dir()
    parent = path.parent

    bundles = list_bundle_paths(path) if exists else []
    online_only = 0
    hydrated = 0
    unknown_hydration = 0
    for b in bundles:
        flag = is_cloud_placeholder(b)
        if flag is True:
            online_only += 1
        elif flag is False:
            hydrated += 1
        else:
            unknown_hydration += 1

    # Never recommend putting the live archive here.
    data_dir_hint = os.environ.get("LOCALAPPDATA")
    archive_outside = (
        str(Path(data_dir_hint) / "slackcache")
        if data_dir_hint
        else "%LOCALAPPDATA%\\slackcache"
    )

    return {
        "ok": True,
        "exchange_dir": str(path),
        "exists": exists,
        "created": create and exists,
        "parent_exists": parent.exists(),
        "env_override": _env_exchange_override() is not None,
        "env_var": EXCHANGE_DIR_ENV,
        "bundle_glob": BUNDLE_GLOB,
        "bundle_count": len(bundles),
        "bundles_hydrated": hydrated,
        "bundles_online_only": online_only,
        "bundles_hydration_unknown": unknown_hydration,
        "pin_recommended": bool(
            online_only > 0 or (sys.platform == "win32" and exists)
        ),
        "pin_hint": _PIN_HINT,
        "rules": {
            "write_once_bundles_only": True,
            "no_archive_db": True,
            "no_shared_cursors": True,
            "local_archive": archive_outside,
        },
        "hydration_caveat": (
            "Online-only placeholders need network to hydrate; pin the folder "
            "for airplane-mode import. Truncated mid-sync files fail closed on decrypt."
            if sys.platform == "win32"
            else "Hydration attributes are Windows-only; treat the folder as a plain path elsewhere."
        ),
    }
