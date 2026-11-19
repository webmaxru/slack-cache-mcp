# -*- mode: python ; coding: utf-8 -*-
# PyInstaller onedir build for slackcache core (PLAN §12.1 / M2).
# Build: uv run pyinstaller packaging/slackcache.spec
# Install output under %LOCALAPPDATA%\slackcache\bin\

from pathlib import Path

block_cipher = None
root = Path(SPECPATH).resolve().parent

a = Analysis(
    [str(root / "src" / "slack_cache_mcp" / "cli.py")],
    pathex=[str(root / "src")],
    binaries=[],
    datas=[
        (
            str(root / "src" / "slack_cache_mcp" / "store" / "sqlite" / "schema.sql"),
            "slack_cache_mcp/store/sqlite",
        ),
    ],
    hiddenimports=[
        "slack_cache_mcp",
        "slack_cache_mcp.cli",
        "slack_cache_mcp.daemon",
        "slack_cache_mcp.vault.store",
        "cryptography",
        "zstandard",
        "watchdog",
        "watchdog.observers",
        "keyring.backends.Windows",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["requests", "httpx", "aiohttp", "selenium", "playwright"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="slackcache",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="slackcache",
)
