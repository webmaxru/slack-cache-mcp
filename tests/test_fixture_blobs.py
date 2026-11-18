"""Cross-check synthetic blobs against the probe decoder path."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from slack_cache_mcp.desktop.chromium.idb_values import decode_blob_file
from slack_cache_mcp.desktop.sanitize import sanitize
from tests.fixture_gen.build_profile import FAKE_XOXC, build_synthetic_profile


def test_synthetic_blobs_decode(tmp_path: Path) -> None:
    profile = build_synthetic_profile(tmp_path / "profile")
    meta = json.loads((profile / "fixture_meta.json").read_text(encoding="utf-8"))
    assert len(meta["blobs"]) == 3
    for entry in meta["blobs"]:
        blob = profile / "IndexedDB" / "https_app.slack.com_0.indexeddb.blob" / entry["blob"]
        info, val = decode_blob_file(blob)
        assert info.error is None, info
        assert isinstance(val, dict)
        assert "messages" in val and "channels" in val
        clean = sanitize(val)
        assert FAKE_XOXC not in json.dumps(clean)
        assert "api_token" not in (clean.get("bootData") or {})


def test_optional_node_parse_blob(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    profile = build_synthetic_profile(tmp_path / "profile")
    meta = json.loads((profile / "fixture_meta.json").read_text(encoding="utf-8"))
    blob = (
        profile
        / "IndexedDB"
        / "https_app.slack.com_0.indexeddb.blob"
        / meta["blobs"][0]["blob"]
    )
    script = Path(__file__).resolve().parents[1] / "docs" / "reference" / "parse_blob.mjs"
    if not script.is_file():
        pytest.skip("parse_blob.mjs missing")
    # snappyjs may be missing — skip soft-fail
    try:
        proc = subprocess.run(
            [node, str(script), str(blob)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except OSError:
        pytest.skip("node failed to run")
    if proc.returncode != 0:
        pytest.skip(f"node decode unavailable: {proc.stderr[:200]}")
    assert "offset" in proc.stdout
