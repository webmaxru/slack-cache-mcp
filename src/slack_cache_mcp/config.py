"""TOML config and per-OS default data paths."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path


def _known_folder_local_appdata() -> Path | None:
    """SHGetKnownFolderPath fallback when LOCALAPPDATA is unset (Windows)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        # Use GUID structure
        from uuid import UUID

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", wintypes.BYTE * 8),
            ]

        def from_uuid(u: UUID) -> GUID:
            g = GUID()
            g.Data1 = u.time_low
            g.Data2 = u.time_mid
            g.Data3 = u.time_hi_version
            for i, b in enumerate(u.bytes[8:]):
                g.Data4[i] = b
            return g

        # FOLDERID_LocalAppData = {F1B32785-...} wait, correct GUID:
        # FOLDERID_LocalAppData = {F1B32785-4FDD-45AE-BB78-4653DF3ED5A9}
        fid = from_uuid(UUID("F1B32785-4FDD-45AE-BB78-4653DF3ED5A9"))
        SHGetKnownFolderPath = ctypes.windll.shell32.SHGetKnownFolderPath
        SHGetKnownFolderPath.argtypes = [
            ctypes.POINTER(GUID),
            wintypes.DWORD,
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        path_ptr = ctypes.c_wchar_p()
        hr = SHGetKnownFolderPath(ctypes.byref(fid), 0, None, ctypes.byref(path_ptr))
        if hr != 0 or not path_ptr.value:
            return None
        return Path(path_ptr.value)
    except Exception:
        return None


def local_appdata() -> Path:
    env = os.environ.get("LOCALAPPDATA")
    if env:
        return Path(env)
    kf = _known_folder_local_appdata()
    if kf:
        return kf
    # Last resort (should not happen on real Windows sessions)
    return Path.home() / "AppData" / "Local"


def roaming_appdata() -> Path:
    env = os.environ.get("APPDATA")
    if env:
        return Path(env)
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            from uuid import UUID

            class GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", wintypes.DWORD),
                    ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD),
                    ("Data4", wintypes.BYTE * 8),
                ]

            def from_uuid(u: UUID) -> GUID:
                g = GUID()
                g.Data1 = u.time_low
                g.Data2 = u.time_mid
                g.Data3 = u.time_hi_version
                for i, b in enumerate(u.bytes[8:]):
                    g.Data4[i] = b
                return g

            # FOLDERID_RoamingAppData
            fid = from_uuid(UUID("3EB685DB-65F9-4CF6-A03A-E3EF65729F3D"))
            path_ptr = ctypes.c_wchar_p()
            hr = ctypes.windll.shell32.SHGetKnownFolderPath(
                ctypes.byref(fid), 0, None, ctypes.byref(path_ptr)
            )
            if hr == 0 and path_ptr.value:
                return Path(path_ptr.value)
        except Exception:
            pass
    return Path.home() / "AppData" / "Roaming"


def default_data_dir() -> Path:
    if sys.platform == "win32":
        return local_appdata() / "slackcache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "slackcache"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "slackcache"
    return Path.home() / ".local" / "share" / "slackcache"


@dataclass
class Settings:
    data_dir: Path = field(default_factory=default_data_dir)
    source_overrides: list[Path] = field(default_factory=list)
    capture_drafts: bool = False
    capture_dms: bool = True
    capture_downloads: bool = False
    capture_http_cache: bool = False
    capture_notifications: bool = False
    capture_ocr: bool = False
    http_cache_max_bytes: int = 50_000_000
    vault_keep: bool = True
    vault_keep_days: int = 30
    poll_interval_s: float = 60.0
    full_pass_interval_s: float = 900.0
    watch_debounce_s: float = 2.0

    @property
    def archive_db(self) -> Path:
        return self.data_dir / "archive.db"

    @property
    def mirror_dir(self) -> Path:
        return self.data_dir / "mirror"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def vault_dir(self) -> Path:
        return self.data_dir / "vault"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"


def load_settings(
    data_dir: Path | None = None,
    source: Path | None = None,
) -> Settings:
    s = Settings()
    if data_dir is not None:
        s.data_dir = Path(data_dir)
    if source is not None:
        s.source_overrides = [Path(source)]
    return s
