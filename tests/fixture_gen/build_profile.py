"""Build a synthetic Slack desktop profile directory for tests."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from tests.fixture_gen.v8_serialize import V8Serializer, wrap_blink_v21, wrap_idb_snappy

FIXTURE_VERSION = "1"

# FAKE tokens for sanitizer tests — never real.
FAKE_XOXC = "xoxc-FAKETOKENVALUE00000000000000000000"
FAKE_XOXD = "xoxd-FAKECOOKIEVALUE0000000000000000000"


def _msg(
    ts: str,
    user: str,
    text: str,
    *,
    channel: str | None = None,
    thread_ts: str | None = None,
    subtype: str | None = None,
    edited: dict[str, Any] | None = None,
    files: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    m: dict[str, Any] = {
        "type": "message",
        "ts": ts,
        "user": user,
        "text": text,
        "__meta__": {"client": True},
        "_hidden_reply": False,
        "_rxn_key": "rxn",
        "_last_read": ts,
        "_subscribed": True,
        "reply_count": 0,
        "reply_users": [],
        "reply_users_count": 0,
        "latest_reply": None,
        "reactions": [],
    }
    if channel:
        m["channel"] = channel
    if thread_ts:
        m["thread_ts"] = thread_ts
    if subtype:
        m["subtype"] = subtype
    if edited:
        m["edited"] = edited
    if files:
        m["files"] = files
    if extra:
        m.update(extra)
    return m


def _workspace_state(
    *,
    workspace_key: str,
    team_id: str,
    user_id: str,
    team_name: str,
    domain: str,
    guest: bool,
) -> dict[str, Any]:
    c_pub = "C0SYNTHPUB1"
    c_priv = "C0SYNTHPRIV"
    c_ext = "C0SYNTHEXT1"
    c_im = "D0SYNTHIM01"
    c_mpim = "G0SYNTHMPIM"

    channels = {
        c_pub: {
            "id": c_pub,
            "name": "general",
            "is_channel": True,
            "is_private": False,
            "is_member": True,
            "topic": {"value": "general topic"},
            "purpose": {"value": "chat"},
            "context_team_id": team_id,
        },
        c_priv: {
            "id": c_priv,
            "name": "secret-project",
            "is_group": True,
            "is_private": True,
            "is_member": True,
            "context_team_id": team_id,
        },
        c_ext: {
            "id": c_ext,
            "name": "guest-shared",
            "is_channel": True,
            "is_private": True,
            "is_ext_shared": True,
            "is_member": True,
            "context_team_id": team_id,
        },
        c_im: {
            "id": c_im,
            "is_im": True,
            "user": "U0OTHER01",
            "is_member": True,
            "context_team_id": team_id,
        },
        c_mpim: {
            "id": c_mpim,
            "name": "mpdm-alice--bob",
            "is_mpim": True,
            "is_member": True,
            "context_team_id": team_id,
        },
    }

    members = {
        user_id: {
            "id": user_id,
            "name": "selfuser",
            "real_name": "Self User",
            "profile": {"display_name": "self", "title": "Dev"},
            "team_id": team_id,
            "is_restricted": guest,
            "is_ultra_restricted": False,
            "is_bot": False,
        },
        "U0OTHER01": {
            "id": "U0OTHER01",
            "name": "alice",
            "real_name": "Alice Example",
            "profile": {"display_name": "alice"},
            "team_id": team_id,
            "is_bot": False,
        },
        "U0BOT0001": {
            "id": "U0BOT0001",
            "name": "robot",
            "real_name": "Robot",
            "is_bot": True,
            "team_id": team_id,
        },
    }

    root_ts = "1763400000.000100"
    reply_ts = "1763400001.000200"
    edited_ts = "1763400002.000300"
    tomb_ts = "1763400003.000400"
    file_ts = "1763400004.000500"

    messages_bucket: dict[str, Any] = {
        root_ts: _msg(
            root_ts,
            user_id,
            "synthetic root message about widgets",
            channel=c_pub,
            extra={"reply_count": 1, "latest_reply": reply_ts, "reply_users": ["U0OTHER01"]},
        ),
        reply_ts: _msg(
            reply_ts,
            "U0OTHER01",
            "synthetic reply agreeing",
            channel=c_pub,
            thread_ts=root_ts,
        ),
        edited_ts: _msg(
            edited_ts,
            user_id,
            "edited synthetic text",
            channel=c_priv,
            edited={"ts": "1763400010.000000", "user": user_id},
        ),
        tomb_ts: _msg(
            tomb_ts,
            user_id,
            "This message was deleted.",
            channel=c_ext,
            subtype="tombstone",
            extra={"hidden": True, "deleted_ts": tomb_ts},
        ),
        file_ts: _msg(
            file_ts,
            user_id,
            "file share",
            channel=c_pub,
            files=[
                {
                    "id": "F0SYNTHFILE",
                    "name": "notes.txt",
                    "title": "Notes",
                    "mimetype": "text/plain",
                    "filetype": "text",
                    "size": 12,
                    "user": user_id,
                    "preview": "hello file",
                }
            ],
        ),
    }

    # IM message
    messages_bucket_im = {
        "1763400100.000100": _msg(
            "1763400100.000100",
            user_id,
            "dm hello",
            channel=c_im,
        )
    }

    threads_slice = {
        c_pub: {
            root_ts: {
                "replies": [
                    messages_bucket[root_ts],
                    messages_bucket[reply_ts],
                ]
            }
        }
    }

    state: dict[str, Any] = {
        "channels": channels,
        "members": members,
        "messages": {
            c_pub: messages_bucket,
            c_priv: {edited_ts: messages_bucket[edited_ts]},
            c_ext: {tomb_ts: messages_bucket[tomb_ts]},
            c_im: messages_bucket_im,
            c_mpim: {},
        },
        "threads": threads_slice,
        "teams": {
            team_id: {"id": team_id, "name": team_name, "domain": domain},
        },
        "selfTeamIds": {"teamId": team_id, "defaultWorkspaceId": team_id},
        "bootData": {
            "team_id": team_id,
            "user_id": user_id,
            "team_url": f"https://{domain}.example.test/",
            "version_ts": "2026-11-18",
            # planted FAKE token — must be scrubbed
            "api_token": FAKE_XOXC,
            "token": FAKE_XOXC,
        },
        "files": {
            "F0SYNTHFILE": {
                "id": "F0SYNTHFILE",
                "name": "notes.txt",
                "title": "Notes",
                "mimetype": "text/plain",
                "size": 12,
                "user": user_id,
            }
        },
    }
    # Stash workspace_key for tests (not a Slack field; used only before sanitize in generator)
    state["_fixture_workspace_key"] = workspace_key
    return state


def build_states() -> list[tuple[str, str, dict[str, Any]]]:
    """Return list of (persist_key, blob_relpath_hint, state)."""
    specs = [
        ("T0SYNTHAAA", "T0SYNTHAAA", "U0SELF0001", "Synth Team A", "synth-a", False),
        ("T0SYNTHBBB", "T0SYNTHBBB", "U0SELF0002", "Synth Team B", "synth-b", False),
        ("E0SYNTHENT", "T0GRIDTEAM", "U0GUEST001", "Enterprise Guest Org", "synth-ent", True),
    ]
    out: list[tuple[str, str, dict[str, Any]]] = []
    for i, (wk, team_id, uid, name, domain, guest) in enumerate(specs):
        state = _workspace_state(
            workspace_key=wk,
            team_id=team_id,
            user_id=uid,
            team_name=name,
            domain=domain,
            guest=guest,
        )
        persist_key = f"persist:slack-client-{wk}-{uid}"
        blob_rel = f"1/{i:02d}/{i + 1}"
        out.append((persist_key, blob_rel, state))
    return out


def write_blob(path: Path, state: dict[str, Any], *, v8_version: int = 16) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Drop helper key before serialize
    clean = {k: v for k, v in state.items() if k != "_fixture_workspace_key"}
    ser = V8Serializer(version=15)  # write as 15; optionally bump header to 16
    v8 = bytearray(ser.dumps(clean))
    if v8_version == 16:
        v8[1] = 16
    blink = wrap_blink_v21(bytes(v8))
    blob = wrap_idb_snappy(blink)
    path.write_bytes(blob)


def build_synthetic_profile(dest: Path, *, wipe: bool = True) -> Path:
    """Create a fake Slack profile tree under `dest` and return it."""
    dest = Path(dest)
    if wipe and dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    # Origin folder name assembled without a contiguous forbidden literal in product code;
    # here in tests it's fine to use the real Chromium folder name.
    idb_level = dest / "IndexedDB" / "https_app.slack.com_0.indexeddb.leveldb"
    idb_blob = dest / "IndexedDB" / "https_app.slack.com_0.indexeddb.blob"
    idb_level.mkdir(parents=True)
    (idb_level / "CURRENT").write_text("MANIFEST-000001\n", encoding="utf-8")
    (idb_level / "MANIFEST-000001").write_bytes(b"\x00")
    (idb_level / "000003.log").write_bytes(b"\x00")
    (idb_level / "LOCK").write_text("held", encoding="utf-8")

    meta = {"fixture_version": FIXTURE_VERSION, "blobs": []}
    for persist_key, blob_rel, state in build_states():
        # strip helper
        blob_path = idb_blob / blob_rel
        write_blob(blob_path, state, v8_version=16)
        meta["blobs"].append(
            {
                "persist_key": persist_key,
                "blob": blob_rel,
                "workspace_key": state["_fixture_workspace_key"],
                "bytes": blob_path.stat().st_size,
            }
        )

    # Local Storage: JSON fixture of localConfig_v2 (parsed by product allowlist function)
    ls = dest / "Local Storage" / "leveldb"
    ls.mkdir(parents=True)
    (ls / "CURRENT").write_text("MANIFEST-000001\n", encoding="utf-8")
    (ls / "LOCK").write_text("held", encoding="utf-8")
    local_config = {
        "teams": {
            "T0SYNTHAAA": {
                "id": "T0SYNTHAAA",
                "name": "Synth Team A",
                "domain": "synth-a",
                "url": "https://synth-a.example.test/",
                "user_id": "U0SELF0001",
                "token": FAKE_XOXC,
                "enterprise_id": None,
            },
            "E0SYNTHENT": {
                "id": "E0SYNTHENT",
                "name": "Enterprise Guest Org",
                "domain": "synth-ent",
                "url": "https://synth-ent.example.test/",
                "user_id": "U0GUEST001",
                "enterprise_id": "E0SYNTHENT",
                "enterprise_name": "Synth Enterprise",
                "token": FAKE_XOXD,
            },
        },
        "api_token": FAKE_XOXC,
    }
    # Side-car JSON for M1 localstate parser (LevelDB encoding deferred)
    (dest / "Local Storage" / "localConfig_v2.json").write_text(
        json.dumps(local_config), encoding="utf-8"
    )

    root_state = {
        "_persist": {},
        "workspaces": {
            "T0SYNTHAAA": {"id": "T0SYNTHAAA", "name": "Synth Team A", "domain": "synth-a"},
            "T0SYNTHBBB": {"id": "T0SYNTHBBB", "name": "Synth Team B", "domain": "synth-b"},
            "E0SYNTHENT": {
                "id": "E0SYNTHENT",
                "name": "Enterprise Guest Org",
                "domain": "synth-ent",
                "teamId": "T0GRIDTEAM",
            },
        },
        "downloads": {},
        "settings": {},
    }
    storage = dest / "storage"
    storage.mkdir(parents=True)
    (storage / "root-state.json").write_text(json.dumps(root_state), encoding="utf-8")
    (dest / "local-settings.json").write_text("{}", encoding="utf-8")

    (dest / "fixture_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return dest


def main() -> None:
    root = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / "profile"
    build_synthetic_profile(root)
    print(f"Wrote synthetic profile to {root}")


if __name__ == "__main__":
    main()
