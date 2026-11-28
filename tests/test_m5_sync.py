"""M5 multi-device sync: identity, bundles, convergence property tests."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from slack_cache_mcp.domain.models import Channel, MessageObservation, Workspace
from slack_cache_mcp.ingest.hlc import HLC, parse_hlc
from slack_cache_mcp.store.sqlite.repo import SCHEMA_VERSION, SqliteRepository
from slack_cache_mcp.sync.apply import apply_change_rows
from slack_cache_mcp.sync.bundle import (
    ensure_local_device,
    export_bundle,
    import_bundles,
    register_peer,
)
from slack_cache_mcp.sync.device import load_or_create_device
from slack_cache_mcp.sync.state_hash import compute_state_hash


def test_hlc_observe_advances_past_remote() -> None:
    from unittest.mock import patch

    # Freeze physical time so now()/observe() share one wall millisecond.
    with patch("slack_cache_mcp.ingest.hlc.time.time", return_value=1_700_000_000.123):
        clock = HLC(device_id="D1")
        first = clock.now()
        w1, c1, _ = parse_hlc(first)
        clock.observe(f"{w1}-{c1 + 7}-D2")
        assert clock.last_wall_ms == w1
        assert clock.counter == c1 + 8
        nxt = clock.now()
        w2, c2, device = parse_hlc(nxt)
        assert device == "D1"
        assert w2 == w1
        assert c2 == c1 + 9


def test_device_identity_persists(tmp_path: Path) -> None:
    data = tmp_path / "data"
    a = load_or_create_device(data, prefer_keyring=False)
    b = load_or_create_device(data, prefer_keyring=False)
    assert a.device_id == b.device_id
    assert a.ed25519_public_b64 == b.ed25519_public_b64
    assert a.age_recipient == b.age_recipient
    assert (data / "device.json").is_file()
    meta = json.loads((data / "device.json").read_text(encoding="utf-8"))
    assert "ed25519_public" in meta
    assert meta["age_recipient"].startswith("age1")


def test_schema_version_5_has_sync_cursors(tmp_path: Path) -> None:
    repo = SqliteRepository(tmp_path / "a.db")
    assert SCHEMA_VERSION == "5"
    row = repo._conn.execute(  # noqa: SLF001
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sync_cursors'"
    ).fetchone()
    assert row is not None
    status = repo.cache_status()
    assert "devices" in status
    assert status["schema_version"] == "5"
    repo.close()


def _seed_facts(
    repo: SqliteRepository,
    *,
    device_id: str,
    hlc: HLC,
    workspace_key: str,
    channel_id: str,
    messages: list[tuple[str, str, str]],
) -> None:
    seen = "2026-11-27T12:00:00Z"
    repo.upsert_workspace(
        Workspace(
            workspace_key=workspace_key,
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
            channel_id=channel_id,
            workspace_key=workspace_key,
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
    for ts, text, sha in messages:
        obs = MessageObservation(
            channel_id=channel_id,
            ts=ts,
            workspace_key=workspace_key,
            user_id="U1",
            thread_ts=None,
            subtype=None,
            text=text,
            search_text=text,
            content_sha256=sha,
            edited_ts=None,
            is_deletion=False,
            is_partial=False,
            source="redux.messages",
            source_rank=100,
            raw={"ts": ts, "text": text, "user": "U1"},
        )
        repo.ingest_observation(
            obs,
            device_id=device_id,
            snapshot_id="snap",
            state_sha256="state",
            seen_at=seen,
            hlc=hlc.now(),
        )


def test_bundle_export_import_roundtrip(tmp_path: Path) -> None:
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    folder = tmp_path / "usb"
    folder.mkdir()

    repo_a = SqliteRepository(dir_a / "archive.db")
    id_a = ensure_local_device(repo_a, dir_a)
    hlc_a = HLC(device_id=id_a.device_id)
    _seed_facts(
        repo_a,
        device_id=id_a.device_id,
        hlc=hlc_a,
        workspace_key="W1",
        channel_id="C1",
        messages=[
            ("1000.000001", "hello from A", "sha-a1"),
            ("1000.000002", "second from A", "sha-a2"),
        ],
    )

    repo_b = SqliteRepository(dir_b / "archive.db")
    id_b = ensure_local_device(repo_b, dir_b)
    # Register mutual age recipients before export.
    register_peer(repo_a, id_b.public_info())
    register_peer(repo_b, id_a.public_info())

    exported = export_bundle(repo_a, identity=id_a, to_dir=folder)
    assert exported["ok"]
    assert exported["exported"] >= 2
    assert Path(exported["path"]).is_file()

    imported = import_bundles(repo_b, identity=id_b, from_dir=folder)
    assert imported["ok"], imported
    assert imported["applied"] >= 2

    # B should now project the same messages.
    msgs = repo_b._conn.execute(  # noqa: SLF001
        "SELECT text FROM messages ORDER BY ts"
    ).fetchall()
    assert [m["text"] for m in msgs] == ["hello from A", "second from A"]

    # Re-import is idempotent (content-sha receipts skip unchanged ciphertext).
    again = import_bundles(repo_b, identity=id_b, from_dir=folder)
    assert again["ok"]
    assert again["applied"] == 0
    assert again.get("files_skipped_known", 0) >= 1 or again["skipped"] >= 2

    repo_a.close()
    repo_b.close()


def test_two_way_bundle_exchange_converges(tmp_path: Path) -> None:
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    folder = tmp_path / "share"
    folder.mkdir()

    repo_a = SqliteRepository(dir_a / "archive.db")
    repo_b = SqliteRepository(dir_b / "archive.db")
    id_a = ensure_local_device(repo_a, dir_a)
    id_b = ensure_local_device(repo_b, dir_b)
    register_peer(repo_a, id_b.public_info())
    register_peer(repo_b, id_a.public_info())

    _seed_facts(
        repo_a,
        device_id=id_a.device_id,
        hlc=HLC(device_id=id_a.device_id),
        workspace_key="W1",
        channel_id="C1",
        messages=[("1.000001", "from-a", "sha-a")],
    )
    _seed_facts(
        repo_b,
        device_id=id_b.device_id,
        hlc=HLC(device_id=id_b.device_id),
        workspace_key="W1",
        channel_id="C1",
        messages=[("1.000002", "from-b", "sha-b")],
    )

    export_bundle(repo_a, identity=id_a, to_dir=folder)
    export_bundle(repo_b, identity=id_b, to_dir=folder)
    import_bundles(repo_a, identity=id_a, from_dir=folder)
    import_bundles(repo_b, identity=id_b, from_dir=folder)

    ha = compute_state_hash(repo_a)
    hb = compute_state_hash(repo_b)
    assert ha == hb
    status = repo_a.cache_status()
    assert len(status["devices"]) >= 2
    assert status["state_hash"] == ha

    repo_a.close()
    repo_b.close()


_msg_st = st.lists(
    st.tuples(
        st.integers(min_value=1, max_value=10_000).map(lambda n: f"{n}.000001"),
        st.text(min_size=1, max_size=40, alphabet=st.characters(whitelist_categories=("L", "N"))),
        st.binary(min_size=8, max_size=8).map(lambda b: b.hex()),
    ),
    min_size=1,
    max_size=8,
    unique_by=lambda t: t[0],
)


@given(
    n_devices=st.integers(min_value=2, max_value=4),
    observations=_msg_st,
    seed=st.integers(min_value=0, max_value=2**32 - 1),
)
@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_convergence_random_order_and_duplicates(
    n_devices: int,
    observations: list[tuple[str, str, str]],
    seed: int,
) -> None:
    """2–4 devices exchange bundles in shuffled order with duplicates → same state_hash."""
    import random

    from slack_cache_mcp.sync.device import DeviceIdentity

    rng = random.Random(seed)
    with tempfile.TemporaryDirectory(prefix="m5-conv-") as td:
        root = Path(td)
        devices: list[tuple[Path, SqliteRepository, DeviceIdentity]] = []
        for i in range(n_devices):
            ddir = root / f"d{i}"
            repo = SqliteRepository(ddir / "archive.db")
            identity = ensure_local_device(repo, ddir)
            devices.append((ddir, repo, identity))

        for _ddir, repo, _ident in devices:
            for _od, _orepo, oident in devices:
                register_peer(repo, oident.public_info())

        for idx, (ts, text, sha) in enumerate(observations):
            _ddir, repo, ident = devices[idx % n_devices]
            hlc = HLC(device_id=ident.device_id)
            _seed_facts(
                repo,
                device_id=ident.device_id,
                hlc=hlc,
                workspace_key="W1",
                channel_id="C1",
                messages=[(ts, text, sha)],
            )

        share = root / "share"
        share.mkdir()
        for _ddir, repo, ident in devices:
            export_bundle(repo, identity=ident, to_dir=share)

        bundle_files = list(share.glob("bundle-*.jsonl.zst.age"))
        assert bundle_files
        for i, path in enumerate(list(bundle_files)):
            dup = share / f"bundle-dup-{i}-{path.name}"
            dup.write_bytes(path.read_bytes())

        order = list(range(n_devices))
        rng.shuffle(order)
        for i in order:
            _ddir, repo, ident = devices[i]
            import_bundles(repo, identity=ident, from_dir=share)
            import_bundles(repo, identity=ident, from_dir=share)

        hashes = [compute_state_hash(repo) for _d, repo, _i in devices]
        assert len(set(hashes)) == 1

        for _d, repo, _i in devices:
            repo.close()


def test_import_rejects_unknown_sender(tmp_path: Path) -> None:
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    folder = tmp_path / "usb"
    folder.mkdir()
    repo_a = SqliteRepository(dir_a / "archive.db")
    repo_b = SqliteRepository(dir_b / "archive.db")
    id_a = ensure_local_device(repo_a, dir_a)
    id_b = ensure_local_device(repo_b, dir_b)
    # A encrypts to B, but B never registered A → import must fail closed.
    register_peer(repo_a, id_b.public_info())
    _seed_facts(
        repo_a,
        device_id=id_a.device_id,
        hlc=HLC(device_id=id_a.device_id),
        workspace_key="W1",
        channel_id="C1",
        messages=[("1.000001", "secret", "sha-s")],
    )
    export_bundle(repo_a, identity=id_a, to_dir=folder)
    result = import_bundles(repo_b, identity=id_b, from_dir=folder)
    assert result["ok"] is False
    assert result["errors"]
    assert "unknown sender" in result["errors"][0]["error"]
    repo_a.close()
    repo_b.close()


def test_apply_rows_sanitizes_payload(tmp_path: Path) -> None:
    repo = SqliteRepository(tmp_path / "a.db")
    identity = ensure_local_device(repo, tmp_path)
    hlc = HLC(device_id=identity.device_id)
    # Workspace first so channel FK / projection workspace resolve.
    _seed_facts(
        repo,
        device_id=identity.device_id,
        hlc=hlc,
        workspace_key="W1",
        channel_id="C1",
        messages=[],
    )
    # Clear outbox so we only apply the crafted row.
    rows = [
        {
            "hlc": hlc.now(),
            "device_id": "PEER1",
            "entity": "message_version",
            "entity_key": "C1|9.000001|sha-x",
            "op": "upsert",
            "payload_json": {
                "channel_id": "C1",
                "ts": "9.000001",
                "workspace_key": "W1",
                "content_sha256": "sha-x",
                "edited_ts": None,
                "is_deletion": False,
                "is_partial": False,
                "source_rank": 100,
                "user_id": "U9",
                "thread_ts": None,
                "subtype": None,
                "text": "token xoxp-should-be-stripped",
                "search_text": "token xoxp-should-be-stripped",
                "raw": {"text": "token xoxp-should-be-stripped", "token": "xoxp-AAAA"},
                "source": "sync",
                "first_seen": "2026-11-27T12:00:00Z",
                "last_seen": "2026-11-27T12:00:00Z",
            },
        }
    ]
    stats = apply_change_rows(repo, rows)
    assert stats["applied"] == 1
    assert repo.scan_for_token_leak() == []
    repo.close()


def test_device_refuses_silent_key_rotation(tmp_path: Path) -> None:
    data = tmp_path / "data"
    first = load_or_create_device(data, prefer_keyring=False)
    secrets = data / "device.secrets"
    assert secrets.is_file()
    secrets.unlink()
    try:
        load_or_create_device(data, prefer_keyring=False)
        raise AssertionError("expected RuntimeError for missing keys")
    except RuntimeError as exc:
        assert first.device_id in str(exc)
        assert "refusing to rotate" in str(exc)


def test_device_refuses_corrupt_metadata(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "device.json").write_text("{not-json", encoding="utf-8")
    try:
        load_or_create_device(data, prefer_keyring=False)
        raise AssertionError("expected RuntimeError for corrupt device.json")
    except RuntimeError as exc:
        assert "malformed" in str(exc)


def test_sparse_directory_lww_order_independent(tmp_path: Path) -> None:
    """name old@1, new@2, null@3 converges regardless of delivery order."""

    def apply_order(order: list[tuple[str, str | None]]) -> str | None:
        repo = SqliteRepository(tmp_path / f"{'-'.join(h for h, _ in order)}.db")
        ensure_local_device(repo, tmp_path / "id")
        for hlc, name in order:
            repo.upsert_workspace(
                Workspace(
                    workspace_key="W1",
                    team_id="T1",
                    enterprise_id=None,
                    name=name,
                    domain=None,
                    self_user_id=None,
                    self_is_guest=False,
                    raw={},
                ),
                hlc,
                "2026-11-27T12:00:00Z",
                hlc_lww=True,
            )
        row = repo._conn.execute(  # noqa: SLF001
            "SELECT name FROM workspaces WHERE workspace_key='W1'"
        ).fetchone()
        name = row["name"] if row else None
        repo.close()
        return name

    # Synthetic HLCs with identical wall; counter encodes order.
    h1 = "1700000000000-1-D1"
    h2 = "1700000000000-2-D1"
    h3 = "1700000000000-3-D1"
    chronological = apply_order([(h1, "old"), (h2, "new"), (h3, None)])
    shuffled = apply_order([(h3, None), (h1, "old"), (h2, "new")])
    assert chronological == "new"
    assert shuffled == "new"


def test_import_rejects_forged_row_author(tmp_path: Path) -> None:
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    folder = tmp_path / "usb"
    folder.mkdir()
    repo_a = SqliteRepository(dir_a / "archive.db")
    repo_b = SqliteRepository(dir_b / "archive.db")
    id_a = ensure_local_device(repo_a, dir_a)
    id_b = ensure_local_device(repo_b, dir_b)
    register_peer(repo_b, id_a.public_info())
    from slack_cache_mcp.sync.crypto import age_encrypt, build_signed_envelope

    envelope = build_signed_envelope(
        identity=id_a,
        from_seq=1,
        to_seq=1,
        rows=[
            {
                "seq": 1,
                "hlc": "1700000000000-1-A",
                "device_id": id_b.device_id,
                "entity": "workspace",
                "entity_key": "W-forged",
                "op": "upsert",
                "payload_json": {
                    "workspace_key": "W-forged",
                    "team_id": "T9",
                    "name": "forged",
                    "self_is_guest": False,
                    "raw": {},
                },
                "payload_sha256": "deadbeef",
            }
        ],
    )
    path = folder / f"bundle-{id_a.device_id}-1-1.jsonl.zst.age"
    path.write_bytes(age_encrypt(envelope, [id_b.age_recipient, id_a.age_recipient]))
    result = import_bundles(repo_b, identity=id_b, from_dir=folder)
    assert result["ok"] is False
    assert any("does not match authenticated sender" in e["error"] for e in result["errors"])
    repo_a.close()
    repo_b.close()


def test_register_peer_resets_push_cursor(tmp_path: Path) -> None:
    repo = SqliteRepository(tmp_path / "a.db")
    identity = ensure_local_device(repo, tmp_path / "a")
    repo.set_sync_cursor("folder", "push", "42")
    repo.set_sync_cursor("usb-stick", "push", "99")
    other = load_or_create_device(tmp_path / "b", prefer_keyring=False)
    register_peer(repo, other.public_info())
    assert repo.get_sync_cursor("folder", "push") == "0"
    assert repo.get_sync_cursor("usb-stick", "push") == "0"
    assert identity.device_id
    repo.close()


def test_export_sequence_bounds_independent_of_entity_order(tmp_path: Path) -> None:
    """from_seq/to_seq use min/max seq even when dependency sort reorders rows."""
    data = tmp_path / "a"
    folder = tmp_path / "usb"
    folder.mkdir()
    repo = SqliteRepository(data / "archive.db")
    identity = ensure_local_device(repo, data)
    hlc = HLC(device_id=identity.device_id)
    # Sighting first (lower seq), then workspace (higher seq) — sort puts workspace first.
    repo.append_entity_change(
        hlc=hlc.now(),
        device_id=identity.device_id,
        entity="sighting",
        entity_key="C1|1.0|dev",
        payload={
            "channel_id": "C1",
            "ts": "1.0",
            "device_id": identity.device_id,
            "source_id": "src",
            "first_seen": "2026-11-27T12:00:00Z",
            "last_seen": "2026-11-27T12:00:00Z",
        },
    )
    repo.append_entity_change(
        hlc=hlc.now(),
        device_id=identity.device_id,
        entity="workspace",
        entity_key="W1",
        payload={
            "workspace_key": "W1",
            "team_id": "T1",
            "name": "ws",
            "self_is_guest": False,
            "raw": {},
            "field_hlcs": {"name": hlc.now()},
        },
    )
    repo.set_sync_cursor("folder", "push", "0")
    # Skip migration purge side-effects for this bounds check.
    repo.set_meta("field_hlcs_policy", "v8-epoch-repair")
    exported = export_bundle(repo, identity=identity, to_dir=folder)
    assert exported["ok"] is True
    assert exported["exported"] >= 2
    assert exported["from_seq"] <= exported["to_seq"]
    assert exported["from_seq"] == 1
    assert exported["to_seq"] >= 2
    assert repo.get_sync_cursor("folder", "push") == str(exported["to_seq"])
    repo.close()


def test_field_hlcs_migration_restores_values_and_stars_clocks(tmp_path: Path) -> None:
    """v8 restores dominating values from log pairs, stars all clocks, emits clock_repair."""
    from slack_cache_mcp.store.sqlite.repo import _UNKNOWN_FIELD_HLC, _load_field_hlcs

    db = tmp_path / "a.db"
    repo = SqliteRepository(db)
    identity = ensure_local_device(repo, tmp_path / "a")
    good = "1700000000000-20-new"
    fabricated = "1700000000000-30-old"
    repo._conn.execute(  # noqa: SLF001
        """
        INSERT INTO workspaces(
          workspace_key, team_id, enterprise_id, name, domain, self_user_id,
          self_is_guest, raw_json, first_seen, last_seen, updated_hlc, field_hlcs_json
        ) VALUES (?, ?, NULL, ?, NULL, ?, 0, '{}', ?, ?, ?, ?)
        """,
        (
            "W1",
            "T1",
            "wrong",
            "U1",
            "2026-11-27T12:00:00Z",
            "2026-11-27T12:00:00Z",
            fabricated,
            json.dumps({"name": fabricated, "team_id": fabricated}),
        ),
    )
    repo.append_entity_change(
        hlc=good,
        device_id=identity.device_id,
        entity="workspace",
        entity_key="W1",
        payload={
            "workspace_key": "W1",
            "team_id": "T1",
            "name": "kept",
            "self_user_id": "U1",
            "self_is_guest": False,
            "raw": {},
            "field_hlcs": {"name": good, "team_id": good},
        },
    )
    repo.set_meta("field_hlcs_policy", "v4-star-all-fields")
    repo._conn.commit()  # noqa: SLF001
    repo.close()

    repo2 = SqliteRepository(db)
    row = repo2._conn.execute(  # noqa: SLF001
        "SELECT field_hlcs_json, name, team_id FROM workspaces WHERE workspace_key='W1'"
    ).fetchone()
    assert row is not None
    assert row["name"] == "kept"
    assert row["team_id"] == "T1"
    clocks = _load_field_hlcs(row["field_hlcs_json"])
    assert clocks.get("team_id") == _UNKNOWN_FIELD_HLC
    assert clocks.get("name") == _UNKNOWN_FIELD_HLC
    assert repo2.directory_clock_epoch() >= 1
    assert repo2.get_meta("field_hlcs_policy") == "v8-pending-outbox-purge"

    rebuilt = repo2.backfill_outbox(device_id=identity.device_id, hlc=good)
    assert rebuilt["workspaces"] >= 1
    assert repo2.get_meta("field_hlcs_policy") == "v8-epoch-repair"
    rows = repo2._conn.execute(  # noqa: SLF001
        """
        SELECT payload_json FROM change_log
        WHERE device_id=? AND entity='workspace' AND entity_key='W1'
        """,
        (identity.device_id,),
    ).fetchall()
    assert rows
    for r in rows:
        payload = json.loads(r["payload_json"])
        assert payload.get("name") == "kept"
        assert payload.get("clock_epoch") == repo2.directory_clock_epoch()
        assert payload.get("clock_repair") is True
        fh = payload.get("field_hlcs") or {}
        assert fabricated not in fh.values()
        assert good not in fh.values()
        assert _UNKNOWN_FIELD_HLC not in fh.values()
    repo2.close()
