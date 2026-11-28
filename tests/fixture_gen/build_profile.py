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
                "preview": "hello file",
            }
        },
        "allThreads": threads_slice,
        "threadSub": {c_pub: {root_ts: {"subscribed": True}}},
        "searchResults": {
            "messages": [
                _msg(
                    "1763400200.000100",
                    "U0OTHER01",
                    "search hit about widgets from elsewhere",
                    channel=c_pub,
                )
            ]
        },
        "channelLatests": {
            c_pub: {
                "ts": root_ts,
                "text": "preview of latest",
                "user": user_id,
            }
        },
        "channelHistory": {
            c_pub: {
                "oldest": root_ts,
                "latest": file_ts,
                "has_more": False,
            }
        },
        "pins": {
            "P0SYNTHPIN1": {
                "id": "P0SYNTHPIN1",
                "channel": c_pub,
                "ts": root_ts,
                "message": messages_bucket[root_ts],
            }
        },
        "saved": {
            "S0SYNTHSAV1": {
                "id": "S0SYNTHSAV1",
                "channel": c_priv,
                "ts": edited_ts,
                "state": "open",
                "date_created": 1763400000.0,
            }
        },
        "stars": {},
        "reactions": {
            c_pub: {
                root_ts: [{"name": "thumbsup", "count": 1, "users": ["U0OTHER01"]}],
            }
        },
        "lists": {},
        "bots": {
            "B0SYNTHBOT1": {
                "id": "B0SYNTHBOT1",
                "name": "synthbot",
                "deleted": False,
                "team_id": team_id,
            }
        },
        "userGroups": {
            "S0GROUP001": {
                "id": "S0GROUP001",
                "name": "eng",
                "handle": "eng",
            }
        },
        "summaryDetail": {},
        "channelSections": {
            "sec1": {"name": "Starred", "channel_ids": [c_pub]},
        },
        "unreadCounts": {c_pub: {"count": 0}},
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
        "downloads": {
            "dl1": {
                "id": "F0DOWNLOAD1",
                "fileName": "memo.txt",
                "downloadPath": str(dest / "downloaded_memo.txt"),
                "mimetype": "text/plain",
                "size": 20,
                "preview_text": "downloaded memo body",
            }
        },
        "settings": {},
    }
    storage = dest / "storage"
    storage.mkdir(parents=True)
    (storage / "root-state.json").write_text(json.dumps(root_state), encoding="utf-8")
    (dest / "local-settings.json").write_text("{}", encoding="utf-8")
    (dest / "downloaded_memo.txt").write_text("downloaded memo body", encoding="utf-8")

    # Canvas IDB side-car
    canvas_db = dest / "IndexedDB" / "canvas-U0SELF0001-T0SYNTHAAA"
    canvas_db.mkdir(parents=True)
    (canvas_db / "records.json").write_text(
        json.dumps(
            [
                {
                    "key": "syncer.Document/Fc0SYNTHCAN1",
                    "store": "canvas",
                    "value": {
                        "_type": "Document",
                        "id": "Fc0SYNTHCAN1",
                        "title": "Synth Canvas",
                        "channel_id": "C0SYNTHPUB1",
                    },
                },
                {
                    "key": "syncer.Section/secA",
                    "store": "canvas",
                    "value": {
                        "_type": "Section",
                        "id": "secA",
                        "document_id": "Fc0SYNTHCAN1",
                        "text": "Canvas section one about widgets",
                    },
                },
                {
                    "key": "syncer.LoadData.Response.Index/Section/Fc0SYNTHCAN1",
                    "store": "canvas",
                    "value": {
                        "_type": "SectionIndex",
                        "document_id": "Fc0SYNTHCAN1",
                        "section_ids": ["secA"],
                    },
                },
            ]
        ),
        encoding="utf-8",
    )

    # Historical IDB side-car message
    hist = dest / "IndexedDB" / "historical-sidecar"
    hist.mkdir(parents=True)
    (hist / "records.json").write_text(
        json.dumps(
            [
                {
                    "key": "msg-hist-1",
                    "store": "default",
                    "state": "deleted",
                    "seq": 42,
                    "value": {
                        "type": "message",
                        "ts": "1763400300.000100",
                        "channel": "C0SYNTHPUB1",
                        "user": "U0OTHER01",
                        "text": "historical leveldb message about widgets",
                    },
                }
            ]
        ),
        encoding="utf-8",
    )

    # Session Storage side-car
    ss = dest / "Session Storage"
    ss.mkdir(parents=True)
    (ss / "CURRENT").write_text("MANIFEST-000001\n", encoding="utf-8")
    (ss / "session_entries.json").write_text(
        json.dumps(
            [
                {
                    "key": "transient-msg",
                    "value": {
                        "type": "message",
                        "ts": "1763400400.000100",
                        "channel": "C0SYNTHPUB1",
                        "user": "U0SELF0001",
                        "text": "session storage transient note",
                    },
                }
            ]
        ),
        encoding="utf-8",
    )

    # Drafts opt-in side-car
    (dest / "Local Storage" / "local_entries.json").write_text(
        json.dumps(
            [
                {
                    "key": "persist-v1::T0SYNTHAAA::U0SELF0001::drafts",
                    "value": {
                        "drafts": {
                            "d1": {
                                "id": "d1",
                                "channel_id": "C0SYNTHPUB1",
                                "text": "unsent draft about widgets",
                            }
                        }
                    },
                }
            ]
        ),
        encoding="utf-8",
    )

    # HTTP cache side-car
    cache = dest / "Cache"
    cache.mkdir(parents=True)
    (cache / "cache_entries.json").write_text(
        json.dumps(
            [
                {
                    "url": "https://files.example.test/files-tmb/abc/thumb.png",
                    "content_type": "image/png",
                    "size": 128,
                    "sha256": "a" * 64,
                    "path": "Cache/Cache_Data/f_000001",
                }
            ]
        ),
        encoding="utf-8",
    )

    # Notifications side-car
    notif = dest / "Notifications"
    notif.mkdir(parents=True)
    (notif / "notifications_win.json").write_text(
        json.dumps(
            [
                {
                    "id": 1,
                    "aumid": "91750D7E.Slack_8she8kybcnzg4",
                    "channel_id": "C0SYNTHPUB1",
                    "user_id": "U0OTHER01",
                    "ts": "1763400500.000100",
                    "text": "notif preview about widgets",
                    "xml": "<toast><text>general</text><text>alice</text>"
                    "<text>notif preview about widgets</text></toast>",
                }
            ]
        ),
        encoding="utf-8",
    )

    (dest / "fixture_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return dest


def build_desktop_missing_reply_profile(dest: Path, *, wipe: bool = True) -> Path:
    """Desktop-shaped profile: thread root has reply_count but no reply body (A/B gap)."""
    dest = Path(dest)
    if wipe and dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    idb_level = dest / "IndexedDB" / "https_app.slack.com_0.indexeddb.leveldb"
    idb_blob = dest / "IndexedDB" / "https_app.slack.com_0.indexeddb.blob"
    idb_level.mkdir(parents=True)
    (idb_level / "CURRENT").write_text("MANIFEST-000001\n", encoding="utf-8")
    (idb_level / "MANIFEST-000001").write_bytes(b"\x00")
    (idb_level / "000003.log").write_bytes(b"\x00")
    (idb_level / "LOCK").write_text("held", encoding="utf-8")

    state = _workspace_state(
        workspace_key="T0SYNTHAAA",
        team_id="T0SYNTHAAA",
        user_id="U0SELF0001",
        team_name="Synth Team A",
        domain="synth-a",
        guest=False,
    )
    # Strip reply body; keep metadata on root (desktop gap Maxim observed).
    pub = state["messages"]["C0SYNTHPUB1"]
    pub.pop("1763400001.000200", None)
    root = pub["1763400000.000100"]
    root["reply_count"] = 1
    root["latest_reply"] = "1763400001.000200"
    # Drop reply bodies from threads/allThreads too (metadata-only desktop gap).
    for slice_name in ("threads", "allThreads"):
        bucket = state.get(slice_name)
        if isinstance(bucket, dict):
            chan = bucket.get("C0SYNTHPUB1")
            if isinstance(chan, dict) and "1763400000.000100" in chan:
                chan["1763400000.000100"] = {
                    "replies": [root],
                    "reply_count": 1,
                    "latest_reply": "1763400001.000200",
                }
    write_blob(idb_blob / "1" / "00" / "1", state, v8_version=16)

    (dest / "storage").mkdir(parents=True)
    (dest / "storage" / "root-state.json").write_text("{}", encoding="utf-8")
    (dest / "local-settings.json").write_text("{}", encoding="utf-8")
    return dest


def build_edge_browser_profile(
    dest: Path,
    *,
    wipe: bool = True,
    with_reply_body: bool = True,
) -> Path:
    """Edge Chromium Default-shaped tree with Slack IDB + credential bait files.

    Only IndexedDB/https_app.slack.com_* is a legitimate source; bait files must never
    be copied by the edge-idb allowlist.
    """
    dest = Path(dest)
    if wipe and dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    idb_level = dest / "IndexedDB" / "https_app.slack.com_0.indexeddb.leveldb"
    idb_blob = dest / "IndexedDB" / "https_app.slack.com_0.indexeddb.blob"
    idb_level.mkdir(parents=True)
    (idb_level / "CURRENT").write_text("MANIFEST-000001\n", encoding="utf-8")
    (idb_level / "MANIFEST-000001").write_bytes(b"\x00")
    (idb_level / "000003.log").write_bytes(b"\x00")
    (idb_level / "LOCK").write_text("held", encoding="utf-8")

    state = _workspace_state(
        workspace_key="T0SYNTHAAA",
        team_id="T0SYNTHAAA",
        user_id="U0SELF0001",
        team_name="Synth Team A",
        domain="synth-a",
        guest=False,
    )
    if with_reply_body:
        # Ensure the reply body Edge A/B found is present (and an Edge-only extra).
        state["messages"]["C0SYNTHPUB1"]["1763400001.000200"] = _msg(
            "1763400001.000200",
            "U0OTHER01",
            "synthetic reply agreeing",
            channel="C0SYNTHPUB1",
            thread_ts="1763400000.000100",
        )
        state["messages"]["C0SYNTHPUB1"]["1763400999.000100"] = _msg(
            "1763400999.000100",
            "U0OTHER01",
            "edge-only reply body about widgets",
            channel="C0SYNTHPUB1",
            thread_ts="1763400000.000100",
        )
    write_blob(idb_blob / "1" / "00" / "1", state, v8_version=16)

    # Unrelated origin under IndexedDB — must not be copied by edge-idb allowlist.
    other = dest / "IndexedDB" / "https_other.example_0.indexeddb.leveldb"
    other.mkdir(parents=True)
    (other / "CURRENT").write_text("MANIFEST-000001\n", encoding="utf-8")
    (other / "secret.log").write_text("should-not-copy", encoding="utf-8")

    # Credential bait (never copied)
    (dest / "Cookies").write_text("d=fake; xoxd-FAKECOOKIE", encoding="utf-8")
    (dest / "Local State").write_text('{"os_crypt": {"encrypted_key": "x"}}', encoding="utf-8")
    (dest / "Network").mkdir(parents=True)
    (dest / "Network" / "Cookies").write_text("network-cookies", encoding="utf-8")
    (dest / "Login Data").write_bytes(b"SQLite format 3\x00")
    ls = dest / "Local Storage" / "leveldb"
    ls.mkdir(parents=True)
    (ls / "CURRENT").write_text("MANIFEST-000001\n", encoding="utf-8")
    (dest / "Local Storage" / "localConfig_v2.json").write_text(
        json.dumps({"token": FAKE_XOXC, "api_token": FAKE_XOXC}),
        encoding="utf-8",
    )
    return dest


def main() -> None:
    root = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / "profile"
    build_synthetic_profile(root)
    print(f"Wrote synthetic profile to {root}")


if __name__ == "__main__":
    main()
