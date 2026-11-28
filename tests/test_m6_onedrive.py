"""M6.0 OneDrive exchange helpers — temp dirs only (no real OneDrive required)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from slack_cache_mcp.domain.models import Channel, MessageObservation, Workspace
from slack_cache_mcp.ingest.hlc import HLC
from slack_cache_mcp.store.sqlite.repo import SqliteRepository
from slack_cache_mcp.sync.bundle import (
    ensure_local_device,
    export_bundle,
    import_bundles,
    register_peer,
)
from slack_cache_mcp.sync.onedrive import (
    EXCHANGE_DIR_ENV,
    default_exchange_dir,
    ensure_exchange_dir,
    exchange_peer_key,
    exchange_status,
    is_cloud_placeholder,
    list_bundle_paths,
)
from slack_cache_mcp.sync.state_hash import compute_state_hash

_FILE_ATTRIBUTE_UNPINNED = 0x00100000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000


def test_default_exchange_dir_under_userprofile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(EXCHANGE_DIR_ENV, raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    got = default_exchange_dir()
    assert got == tmp_path / "OneDrive - Microsoft" / "Projects" / "slackcache"


def test_exchange_dir_env_override(tmp_path: Path, monkeypatch) -> None:
    custom = tmp_path / "usb-stick" / "bundles"
    monkeypatch.setenv(EXCHANGE_DIR_ENV, str(custom))
    assert default_exchange_dir() == custom


def test_empty_env_override_is_ignored(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv(EXCHANGE_DIR_ENV, "   ")
    assert default_exchange_dir() == (
        tmp_path / "OneDrive - Microsoft" / "Projects" / "slackcache"
    )
    # Empty override must not bypass the OneDrive-root existence check.
    try:
        ensure_exchange_dir(create=True)
        raise AssertionError("expected FileNotFoundError")
    except FileNotFoundError:
        pass


def test_ensure_exchange_dir_creates_readme(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(EXCHANGE_DIR_ENV, str(tmp_path / "exchange"))
    path = ensure_exchange_dir(create=True)
    assert path.is_dir()
    readme = path / "README.txt"
    assert readme.is_file()
    text = readme.read_text(encoding="utf-8")
    assert "archive.db" in text
    assert "Always keep on this device" in text
    # Idempotent: do not clobber an edited README.
    readme.write_text("custom", encoding="utf-8")
    ensure_exchange_dir(create=True)
    assert readme.read_text(encoding="utf-8") == "custom"


def test_exchange_status_counts_bundles(tmp_path: Path, monkeypatch) -> None:
    exchange = tmp_path / "ex"
    exchange.mkdir()
    (exchange / "bundle-A-1-2.jsonl.zst.age").write_bytes(b"cipher")
    (exchange / "noise.txt").write_text("x", encoding="utf-8")
    monkeypatch.setenv(EXCHANGE_DIR_ENV, str(exchange))
    status = exchange_status()
    assert status["exists"] is True
    assert status["bundle_count"] == 1
    assert status["rules"]["no_archive_db"] is True
    assert status["rules"]["no_shared_cursors"] is True
    assert list_bundle_paths(exchange)[0].name.startswith("bundle-")


def test_is_cloud_placeholder_regular_file(tmp_path: Path) -> None:
    f = tmp_path / "local.age"
    f.write_bytes(b"abc")
    flag = is_cloud_placeholder(f)
    # On Windows a normal file is hydrated (False); elsewhere unknown (None).
    assert flag in (False, None)


def _seed_one_message(repo: SqliteRepository, *, device_id: str) -> None:
    hlc = HLC(device_id=device_id)
    seen = "2026-11-28T12:00:00Z"
    repo.upsert_workspace(
        Workspace(
            workspace_key="W1",
            team_id="T1",
            enterprise_id=None,
            name="ws",
            domain="example",
            self_user_id="U1",
            self_is_guest=False,
            raw={"name": "ws"},
        ),
        hlc.now(),
        seen,
        device_id=device_id,
    )
    repo.upsert_channel(
        Channel(
            channel_id="C1",
            workspace_key="W1",
            context_team_id="T1",
            name="general",
            kind="channel",
            is_ext_shared=False,
            is_archived=False,
            is_member=True,
            topic=None,
            purpose=None,
            im_user_id=None,
            raw={"name": "general"},
        ),
        hlc.now(),
        seen,
        device_id=device_id,
    )
    obs = MessageObservation(
        channel_id="C1",
        ts="1700000000.000100",
        workspace_key="W1",
        user_id="U1",
        thread_ts=None,
        subtype=None,
        text="hello from A",
        search_text="hello from A",
        content_sha256="sha-a1",
        edited_ts=None,
        is_deletion=False,
        is_partial=False,
        source="redux.messages",
        source_rank=100,
        raw={"ts": "1700000000.000100", "text": "hello from A", "user": "U1"},
    )
    repo.ingest_observation(
        obs,
        device_id=device_id,
        snapshot_id="snap",
        state_sha256="state",
        seen_at=seen,
        hlc=hlc.now(),
    )


def test_two_device_folder_living_via_exchange(tmp_path: Path, monkeypatch) -> None:
    """Device A push → shared temp exchange → device B pull converges (CI-safe)."""
    exchange = tmp_path / "OneDrive - Microsoft" / "Projects" / "slackcache"
    monkeypatch.setenv(EXCHANGE_DIR_ENV, str(exchange))
    ensure_exchange_dir(create=True)

    a_data = tmp_path / "a"
    b_data = tmp_path / "b"
    a_repo = SqliteRepository(a_data / "archive.db")
    b_repo = SqliteRepository(b_data / "archive.db")
    a_id = ensure_local_device(a_repo, a_data)
    b_id = ensure_local_device(b_repo, b_data)
    register_peer(a_repo, b_id.public_info())
    register_peer(b_repo, a_id.public_info())

    _seed_one_message(a_repo, device_id=a_id.device_id)

    pushed = export_bundle(a_repo, identity=a_id, to_dir=default_exchange_dir())
    assert pushed["ok"] is True
    assert pushed["exported"] >= 1
    assert exchange_status()["bundle_count"] >= 1

    pulled = import_bundles(b_repo, identity=b_id, from_dir=default_exchange_dir())
    assert pulled["ok"] is True
    assert pulled["files_imported"] >= 1
    assert compute_state_hash(a_repo) == compute_state_hash(b_repo)

    cursors = b_repo.list_sync_cursors()
    assert any(c["direction"] == "pull" for c in cursors)

    a_repo.close()
    b_repo.close()


def test_cli_push_pull_aliases_use_env_exchange(tmp_path: Path, monkeypatch) -> None:
    """Smoke the Typer aliases against a temp exchange (no real OneDrive)."""
    from typer.testing import CliRunner

    from slack_cache_mcp.cli import app

    exchange = tmp_path / "ex"
    monkeypatch.setenv(EXCHANGE_DIR_ENV, str(exchange))
    a_data = tmp_path / "cli-a"
    b_data = tmp_path / "cli-b"

    runner = CliRunner()
    assert runner.invoke(app, ["sync", "init", "--data-dir", str(a_data)]).exit_code == 0
    assert runner.invoke(app, ["sync", "init", "--data-dir", str(b_data)]).exit_code == 0

    peer_a = tmp_path / "a.json"
    peer_b = tmp_path / "b.json"
    peer_a.write_text(
        runner.invoke(app, ["sync", "show-device", "--data-dir", str(a_data)]).output,
        encoding="utf-8",
    )
    peer_b.write_text(
        runner.invoke(app, ["sync", "show-device", "--data-dir", str(b_data)]).output,
        encoding="utf-8",
    )
    assert (
        runner.invoke(
            app,
            ["sync", "peers", "add", "--from", str(peer_b), "--data-dir", str(a_data)],
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app,
            ["sync", "peers", "add", "--from", str(peer_a), "--data-dir", str(b_data)],
        ).exit_code
        == 0
    )

    repo = SqliteRepository(a_data / "archive.db")
    identity = ensure_local_device(repo, a_data)
    _seed_one_message(repo, device_id=identity.device_id)
    repo.close()

    push = runner.invoke(app, ["sync", "push", "--data-dir", str(a_data)])
    assert push.exit_code == 0, push.output
    push_json = json.loads(push.output)
    assert push_json["exchange_dir"] == str(exchange)
    assert any(exchange.glob("bundle-*.jsonl.zst.age"))

    status = runner.invoke(app, ["sync", "status", "--data-dir", str(a_data)])
    assert status.exit_code == 0, status.output
    status_json = json.loads(status.output)
    assert status_json["exchange"]["bundle_count"] >= 1
    assert status_json["exchange"]["rules"]["no_archive_db"] is True

    pull = runner.invoke(app, ["sync", "pull", "--data-dir", str(b_data)])
    assert pull.exit_code == 0, pull.output
    pull_json = json.loads(pull.output)
    assert pull_json["files_imported"] >= 1


def test_status_missing_exchange_without_create(tmp_path: Path, monkeypatch) -> None:
    missing = tmp_path / "nope" / "slackcache"
    monkeypatch.setenv(EXCHANGE_DIR_ENV, str(missing))
    st = exchange_status(create=False)
    assert st["exists"] is False
    assert st["bundle_count"] == 0
    assert not missing.exists()
    assert default_exchange_dir() == missing
    assert EXCHANGE_DIR_ENV in os.environ


def test_default_create_requires_onedrive_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(EXCHANGE_DIR_ENV, raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    # No OneDrive - Microsoft directory under the fake profile.
    try:
        ensure_exchange_dir(create=True)
        raise AssertionError("expected FileNotFoundError")
    except FileNotFoundError as exc:
        assert "OneDrive root not found" in str(exc)
    assert not (tmp_path / "OneDrive - Microsoft").exists()


def test_exchange_peer_key_scopes_default_folder(tmp_path: Path) -> None:
    a = exchange_peer_key(tmp_path / "ex-a")
    b = exchange_peer_key(tmp_path / "ex-b")
    assert a.startswith("folder:")
    assert a != b
    assert exchange_peer_key(tmp_path / "ex-a", peer="usb") == "usb"


def test_full_reexport_retains_prior_bundles(tmp_path: Path) -> None:
    exchange = tmp_path / "ex"
    exchange.mkdir()
    data = tmp_path / "data"
    repo = SqliteRepository(data / "archive.db")
    identity = ensure_local_device(repo, data)
    _seed_one_message(repo, device_id=identity.device_id)
    first = export_bundle(repo, identity=identity, to_dir=exchange, peer="folder")
    assert first["ok"]
    first_path = Path(first["path"])
    assert first_path.is_file()
    # Reset push cursor (as peers add does) and export again.
    repo.reset_push_cursors()
    second = export_bundle(repo, identity=identity, to_dir=exchange, peer="folder")
    assert second["ok"]
    assert Path(second["path"]).is_file()
    assert first_path.is_file(), "prior immutable bundle must remain"
    assert first_path != Path(second["path"])
    assert len(list(exchange.glob("bundle-*.jsonl.zst.age"))) >= 2
    repo.close()


def test_import_skips_already_processed_ciphertext(tmp_path: Path) -> None:
    exchange = tmp_path / "ex"
    exchange.mkdir()
    a_data = tmp_path / "a"
    b_data = tmp_path / "b"
    a_repo = SqliteRepository(a_data / "archive.db")
    b_repo = SqliteRepository(b_data / "archive.db")
    a_id = ensure_local_device(a_repo, a_data)
    b_id = ensure_local_device(b_repo, b_data)
    register_peer(a_repo, b_id.public_info())
    register_peer(b_repo, a_id.public_info())
    _seed_one_message(a_repo, device_id=a_id.device_id)
    export_bundle(a_repo, identity=a_id, to_dir=exchange)
    first = import_bundles(b_repo, identity=b_id, from_dir=exchange)
    assert first["files_imported"] >= 1
    second = import_bundles(b_repo, identity=b_id, from_dir=exchange)
    assert second["files_imported"] == 0
    assert second["files_skipped_known"] >= 1
    assert second["ok"] is True
    a_repo.close()
    b_repo.close()


def test_peer_add_bumps_generation_and_blocks_stale_advance(
    tmp_path: Path, monkeypatch
) -> None:
    """Peer-add mid-export (cursor already 0) must not advance the push cursor."""
    exchange = tmp_path / "ex"
    exchange.mkdir()
    a_data = tmp_path / "a"
    b_data = tmp_path / "b"
    a_repo = SqliteRepository(a_data / "archive.db")
    b_repo = SqliteRepository(b_data / "archive.db")
    a_id = ensure_local_device(a_repo, a_data)
    b_id = ensure_local_device(b_repo, b_data)
    _seed_one_message(a_repo, device_id=a_id.device_id)

    gen_before = a_repo.get_sync_peer_generation()
    assert gen_before == 0

    real_encrypt = None
    from slack_cache_mcp.sync import bundle as bundle_mod

    real_encrypt = bundle_mod.age_encrypt

    def _encrypt_and_add_peer(envelope: bytes, recipients: list[str]) -> bytes:
        # Concurrent peers add while export is encrypting.
        register_peer(a_repo, b_id.public_info())
        assert a_repo.get_sync_peer_generation() == gen_before + 1
        return real_encrypt(envelope, recipients)

    monkeypatch.setattr(bundle_mod, "age_encrypt", _encrypt_and_add_peer)
    result = export_bundle(a_repo, identity=a_id, to_dir=exchange, peer="folder")
    assert result["ok"]
    assert result.get("peers_changed") is True
    assert result.get("cursor_advanced") is False
    assert a_repo.get_sync_cursor("folder", "push") == "0"
    a_repo.close()
    b_repo.close()


def test_unpinned_attribute_is_not_online_only(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "local.age"
    f.write_bytes(b"abc")

    def _unpinned(_path: Path) -> int:
        return _FILE_ATTRIBUTE_UNPINNED

    monkeypatch.setattr(
        "slack_cache_mcp.sync.onedrive.file_attributes",
        _unpinned,
    )
    assert is_cloud_placeholder(f) is False

    def _recall(_path: Path) -> int:
        return _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS

    monkeypatch.setattr(
        "slack_cache_mcp.sync.onedrive.file_attributes",
        _recall,
    )
    assert is_cloud_placeholder(f) is True


def test_load_json_file_accepts_utf16_le_bom(tmp_path: Path) -> None:
    """PowerShell ``>`` writes UTF-16 LE BOM; peers add must still parse it."""
    from slack_cache_mcp.cli import _load_json_file

    payload = {
        "device_id": "01TESTDEVICE00000000000001",
        "label": "peer",
        "ed25519_public": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "age_recipient": "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqun6ul0",
    }
    path = tmp_path / "peer-utf16.json"
    path.write_bytes(json.dumps(payload, indent=2).encode("utf-16"))  # includes BOM
    assert path.read_bytes()[:2] == b"\xff\xfe"
    assert _load_json_file(path) == payload

    utf8_bom = tmp_path / "peer-utf8-bom.json"
    utf8_bom.write_bytes(b"\xef\xbb\xbf" + json.dumps(payload).encode("utf-8"))
    assert _load_json_file(utf8_bom) == payload


def test_peers_add_cli_accepts_utf16_peer_json(
    tmp_path: Path, monkeypatch
) -> None:
    from typer.testing import CliRunner

    from slack_cache_mcp.cli import app

    # Avoid writing sync private keys into the real OS keyring during CLI init.
    monkeypatch.setattr(
        "slack_cache_mcp.sync.device._store_keyring_secret",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "slack_cache_mcp.sync.device._load_keyring_secret",
        lambda *_a, **_k: None,
    )

    a_data = tmp_path / "a"
    b_data = tmp_path / "b"
    runner = CliRunner()
    assert runner.invoke(app, ["sync", "init", "--data-dir", str(a_data)]).exit_code == 0
    assert runner.invoke(app, ["sync", "init", "--data-dir", str(b_data)]).exit_code == 0
    peer_b = tmp_path / "b-utf16.json"
    show = runner.invoke(app, ["sync", "show-device", "--data-dir", str(b_data)])
    assert show.exit_code == 0
    peer_b.write_bytes(show.output.encode("utf-16"))
    result = runner.invoke(
        app,
        ["sync", "peers", "add", "--from", str(peer_b), "--data-dir", str(a_data)],
    )
    assert result.exit_code == 0, result.output
    assert '"ok": true' in result.output
