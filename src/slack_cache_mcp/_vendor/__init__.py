"""Vendored third-party packages (MIT). Pins in PINS.txt."""

from __future__ import annotations

import sys
import types
from pathlib import Path

_VENDOR_ROOT = Path(__file__).resolve().parent

# ccl_chromium_reader's package __init__ imports brotli/zstd for HTTP-cache
# body decompression. We never read cache bodies in M1, so stub if absent.
for _name in ("brotli", "zstd"):
    try:
        __import__(_name)
    except ImportError:
        sys.modules[_name] = types.ModuleType(_name)

# Ensure vendor packages are importable as top-level names (ccl_chromium_reader,
# ccl_simplesnappy) without installing them.
_vendor_str = str(_VENDOR_ROOT)
if _vendor_str not in sys.path:
    sys.path.insert(0, _vendor_str)
