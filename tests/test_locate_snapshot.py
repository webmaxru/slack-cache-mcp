"""locate + snapshot tests with fake env / filesystem."""

from __future__ import annotations

from pathlib import Path

from slack_cache_mcp.desktop.locate import discover_profiles
from slack_cache_mcp.desktop.snapshot import snapshot_profile
from tests.fixture_gen.build_profile import build_synthetic_profile


def test_locate_override(tmp_path: Path) -> None:
    profile = build_synthetic_profile(tmp_path / "Slack")
    found = discover_profiles(overrides=[profile])
    assert len(found) == 1
    assert found[0].valid
    assert found[0].kind == "override"


def test_locate_windows_store_and_direct(tmp_path: Path) -> None:
    local = tmp_path / "Local"
    roaming = tmp_path / "Roaming"
    store = (
        local
        / "Packages"
        / "91750D7E.Slack_8she8kybcnzg4"
        / "LocalCache"
        / "Roaming"
        / "Slack"
    )
    build_synthetic_profile(store)
    direct = roaming / "Slack"
    build_synthetic_profile(direct)
    env = {"LOCALAPPDATA": str(local), "APPDATA": str(roaming)}
    found = discover_profiles(env=env, platform="win32")
    kinds = {p.kind for p in found if p.valid}
    assert "win-store" in kinds
    assert "win-direct" in kinds
    assert sum(1 for p in found if p.primary) == 1


def test_snapshot_skips_lock_and_is_idempotent(tmp_path: Path) -> None:
    src = build_synthetic_profile(tmp_path / "src")
    mirror = tmp_path / "mirror"
    opens: list[tuple] = []

    real_open = open

    def tracking_open(path, mode="r", *a, **kw):  # type: ignore[no-untyped-def]
        opens.append((str(path), mode))
        return real_open(path, mode, *a, **kw)

    s1 = snapshot_profile(src, mirror, open_func=tracking_open, require_stable_blobs=False)
    assert s1.skipped_lock >= 1
    assert s1.copied > 0
    # no write modes on source
    src_s = str(src.resolve())
    for path, mode in opens:
        if Path(path).resolve().as_posix().startswith(Path(src_s).as_posix()):
            assert mode in ("rb", "r")

    s2 = snapshot_profile(src, mirror, open_func=tracking_open, require_stable_blobs=False)
    assert s2.copied == 0
    assert s2.skipped_unchanged > 0
