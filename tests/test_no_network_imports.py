"""AST scan: product code (excl. sync/) must not import network clients or embed Slack URLs/tokens."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "slack_cache_mcp"

FORBIDDEN_MODULES = {
    "requests",
    "httpx",
    "httpx2",
    "aiohttp",
    "urllib3",
    "urllib.request",
    "http.client",
    "websockets",
    "grpc",
    "slack_sdk",
    "slack_bolt",
    "boto3",
    "paramiko",
    "ftplib",
    "smtplib",
    "webbrowser",
    "selenium",
    "playwright",
    "pychrome",
    "pywinauto",
    "pyautogui",
}

# Allowlisted urllib.parse only inside vendored ccl.
ALLOW_URLLIB_PARSE_PREFIXES = (
    "src/slack_cache_mcp/_vendor/ccl_chromium_reader/",
    "src\\slack_cache_mcp\\_vendor\\ccl_chromium_reader\\",
)

SLACK_URL_RE = re.compile(
    r"(slack\.com|slack-edge\.com|slack-msgs\.com|xox[a-z]-)",
    re.IGNORECASE,
)

# Constructed permalink hosts are forbidden as literals in product code except
# the documented citation builder may assemble strings without these domains
# as contiguous literals matching the regex — we ban the domains themselves.
SKIP_DIRS = {"_vendor", "sync", "__pycache__"}


def _iter_py_files() -> list[Path]:
    out: list[Path] = []
    for path in SRC.rglob("*.py"):
        parts = set(path.relative_to(SRC).parts)
        if parts & SKIP_DIRS or any(p in SKIP_DIRS for p in path.relative_to(SRC).parts):
            # Skip anything under _vendor/ or sync/
            rel_parts = path.relative_to(SRC).parts
            if rel_parts and rel_parts[0] in {"_vendor", "sync"}:
                continue
        out.append(path)
    return out


def _module_name_from_import(node: ast.AST) -> list[str]:
    names: list[str] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            names.append(alias.name)
    elif isinstance(node, ast.ImportFrom):
        if node.module:
            names.append(node.module)
    return names


def test_no_forbidden_network_imports() -> None:
    offenders: list[str] = []
    for path in _iter_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for mod in _module_name_from_import(node):
                top = mod.split(".")[0]
                full = mod
                if full in FORBIDDEN_MODULES or top in FORBIDDEN_MODULES:
                    offenders.append(f"{path}: import {mod}")
                if full == "urllib.request" or mod.startswith("urllib.request"):
                    offenders.append(f"{path}: import {mod}")
                if full.startswith("http.client") or mod == "http.client":
                    offenders.append(f"{path}: import {mod}")
    assert not offenders, "Forbidden network imports:\n" + "\n".join(offenders)


def test_no_slack_url_or_token_literals() -> None:
    offenders: list[str] = []
    for path in _iter_py_files():
        # Citation permalink construction uses domain fragments carefully;
        # ban contiguous literals matching the regex.
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if SLACK_URL_RE.search(node.value):
                    offenders.append(f"{path}:{node.lineno}: {node.value[:80]!r}")
    assert not offenders, "Forbidden Slack URL/token literals:\n" + "\n".join(offenders)


def test_vendor_urllib_parse_is_only_parse() -> None:
    """ccl may import urllib.parse; urllib.request must not appear in vendor either."""
    vendor = SRC / "_vendor"
    for path in vendor.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for mod in _module_name_from_import(node):
                assert "urllib.request" not in mod
                assert mod.split(".")[0] not in {
                    "requests",
                    "httpx",
                    "aiohttp",
                    "slack_sdk",
                }
