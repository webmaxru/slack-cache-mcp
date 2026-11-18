"""
slack_cache_probe.py - OFFLINE, READ-ONLY probe of the Slack desktop app's local cache.

* Makes NO network calls (no socket/http imports at all).
* Never opens the live Slack folder with a DB library: it first copies files
  (skipping LOCK) into a snapshot dir, then parses only the copy.
* Never prints tokens/cookies; output is counts/structure only, plus channel and
  workspace names. All output passes through a token scrubber.

Usage:
  python slack_cache_probe.py snapshot <slack_data_dir> <snapshot_dir>
  python slack_cache_probe.py analyze  <snapshot_dir> [report.json]
"""
import sys, os, io, re, json, shutil, time, types, pathlib, datetime, collections, traceback

# ccl_chromium_reader's package __init__ pulls in its HTTP-cache module, which imports
# brotli/zstd for body decompression. We never read cache bodies, so stub them if absent.
for _m in ("brotli", "zstd"):
    try:
        __import__(_m)
    except ImportError:
        sys.modules[_m] = types.ModuleType(_m)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "vendor"))

TOKEN_RE = re.compile(r"xox[a-z]-[A-Za-z0-9%\-_.]+|xoxd-[A-Za-z0-9%\-_.]+", re.I)
def scrub(s):
    return TOKEN_RE.sub("<REDACTED-TOKEN>", s)
SENSITIVE_KEYS = {"token", "tokens", "api_token", "xoxc", "xoxd", "cookie", "cookies", "d", "d-s",
                  "enterprise_api_token", "session", "auth"}

def out(*a):
    print(scrub(" ".join(str(x) for x in a)), flush=True)

# ---------------------------------------------------------------- snapshot
def copy_file(src, dst, tries=6):
    for i in range(tries):
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            return None
        except FileNotFoundError:
            return "vanished"
        except PermissionError as e:   # Windows sharing violation
            err = e
            time.sleep(0.5 * (i + 1))
        except OSError as e:
            err = e
            time.sleep(0.5 * (i + 1))
    return f"failed: {type(err).__name__}: {getattr(err, 'winerror', '')} {err.strerror if hasattr(err, 'strerror') else ''}"

def copy_tree(src_root, dst_root, stats, max_bytes=None):
    if not src_root.exists():
        stats["missing"].append(str(src_root.name)); return
    total = 0
    for p in src_root.rglob("*"):
        if p.is_dir():
            continue
        if p.name == "LOCK":
            stats["skipped_lock"] += 1; continue
        try:
            sz = p.stat().st_size
        except OSError:
            sz = 0
        if max_bytes and total + sz > max_bytes:
            stats["skipped_size_cap"] += 1; continue
        r = copy_file(p, dst_root / p.relative_to(src_root))
        if r is None:
            stats["copied"] += 1; stats["bytes"] += sz; total += sz
        else:
            stats["errors"].append(f"{p.relative_to(src_root)}: {r}")

def snapshot(slack_dir, snap_dir):
    slack_dir = pathlib.Path(slack_dir); snap_dir = pathlib.Path(snap_dir)
    snap_dir.mkdir(parents=True, exist_ok=True)
    stats = dict(copied=0, bytes=0, skipped_lock=0, skipped_size_cap=0, errors=[], missing=[])
    t0 = time.time()
    copy_tree(slack_dir / "IndexedDB", snap_dir / "IndexedDB", stats)
    copy_tree(slack_dir / "Local Storage" / "leveldb", snap_dir / "LocalStorage_leveldb", stats)
    copy_tree(slack_dir / "Service Worker" / "CacheStorage", snap_dir / "SW_CacheStorage", stats,
              max_bytes=400 * 1024 * 1024)
    for rel in (("storage", "root-state.json"),):
        src = slack_dir.joinpath(*rel)
        if src.exists():
            r = copy_file(src, snap_dir / rel[-1])
            if r: stats["errors"].append(f"{rel[-1]}: {r}")
            else: stats["copied"] += 1
        else:
            stats["missing"].append("/".join(rel))
    stats["seconds"] = round(time.time() - t0, 1)
    # inventory of the live top-level folder (names + sizes only)
    inv = {}
    for p in sorted(slack_dir.iterdir()):
        try:
            if p.is_dir():
                n = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                inv[p.name + "/"] = n
            else:
                inv[p.name] = p.stat().st_size
        except OSError as e:
            inv[p.name] = f"err {e.__class__.__name__}"
    stats["live_inventory_bytes"] = inv
    idb = slack_dir / "IndexedDB"
    stats["live_indexeddb_entries"] = sorted(x.name for x in idb.iterdir()) if idb.exists() else []
    out(json.dumps(stats, indent=1))
    return stats

# ---------------------------------------------------------------- helpers
def paris_dt(ts):
    try:
        f = float(ts)
    except (TypeError, ValueError):
        return None
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Paris"); label = "Europe/Paris"
    except Exception:
        tz = None; label = "PC-local"
    d = datetime.datetime.fromtimestamp(f, tz) if tz else datetime.datetime.fromtimestamp(f).astimezone()
    return d.strftime("%Y-%m-%d %H:%M ") + (label if tz else d.strftime("%Z(PC-local)"))

def is_msg(o):
    return isinstance(o, dict) and ("ts" in o) and (
        o.get("type") == "message" or isinstance(o.get("text"), str) or "subtype" in o or "user" in o or "reply_count" in o)

def shape(o, depth=0):
    """keys-only shape, never values"""
    if isinstance(o, dict):
        if depth >= 1:
            return "{%d keys}" % len(o)
        return {k: shape(v, depth + 1) for k, v in list(o.items())[:40]}
    if isinstance(o, list):
        return "[list len %d]" % len(o)
    return type(o).__name__

def as_dict(v):
    if isinstance(v, dict): return v
    if isinstance(v, list): return {str(i): x for i, x in enumerate(v)}
    return {}

def walk_msgs(node, chan, found, depth=0, seen=None):
    if seen is None: seen = set()
    if depth > 8 or id(node) in seen: return
    if isinstance(node, (dict, list)): seen.add(id(node))
    if isinstance(node, dict):
        if is_msg(node):
            c = node.get("channel") or chan
            found[(c, str(node.get("ts")))] = node
        for k, v in node.items():
            if k in ("attachments", "blocks", "files", "metadata"): continue
            nc = k if isinstance(k, str) and k[:1] in "CDG" and len(k) >= 9 else chan
            walk_msgs(v, nc, found, depth + 1, seen)
    elif isinstance(node, list):
        for v in node: walk_msgs(v, chan, found, depth + 1, seen)

FLAG_KEYS_CH = ["is_channel", "is_group", "is_im", "is_mpim", "is_private", "is_shared", "is_ext_shared",
                "is_org_shared", "is_pending_ext_shared", "is_archived", "is_member", "is_general"]
FLAG_KEYS_MEM = ["is_restricted", "is_ultra_restricted", "is_stranger", "is_bot", "deleted", "is_external",
                 "is_invited_user"]

def analyze_state(state, label):
    r = {"label": label}
    r["top_level_keys"] = {k: (len(v) if isinstance(v, (dict, list)) else type(v).__name__) for k, v in state.items()}
    sti = as_dict(state.get("selfTeamIds"))
    boot = as_dict(state.get("bootData"))
    r["team_id"] = sti.get("teamId") or sti.get("defaultWorkspaceId") or boot.get("team_id")
    r["self_user_id"] = boot.get("user_id")
    teams = as_dict(state.get("teams"))
    if r["team_id"] and isinstance(teams.get(r["team_id"]), dict):
        t = teams[r["team_id"]]; r["team_name"] = t.get("name"); r["team_domain"] = t.get("domain")
    r["teams_known"] = len(teams)
    chans = {k: v for k, v in as_dict(state.get("channels")).items() if isinstance(v, dict)}
    r["channels_known"] = len(chans)
    fc = collections.Counter()
    for c in chans.values():
        for f in FLAG_KEYS_CH:
            if c.get(f) is True: fc[f] += 1
    r["channel_flags"] = dict(fc)
    msgs_slice = as_dict(state.get("messages"))
    per_chan = {}
    all_ts = []; replies = 0; parents = 0; reply_count_sum = 0; sample = None
    subtypes = collections.Counter()
    for cid, bucket in msgs_slice.items():
        found = {}
        walk_msgs(bucket, cid, found)
        if not found: continue
        per_chan[cid] = len(found)
        for (c, ts), m in found.items():
            all_ts.append(float(ts)) if re.match(r"^\d+(\.\d+)?$", ts) else None
            tts = m.get("thread_ts")
            if tts and str(tts) != ts: replies += 1
            if isinstance(m.get("reply_count"), (int, float)) and m.get("reply_count") > 0:
                parents += 1; reply_count_sum += int(m["reply_count"])
            if m.get("subtype"): subtypes[str(m["subtype"])] += 1
            if sample is None and isinstance(m.get("text"), str): sample = sorted(m.keys())
    r["channels_with_cached_messages"] = len(per_chan)
    r["total_cached_messages"] = sum(per_chan.values())
    r["oldest_message"] = paris_dt(min(all_ts)) if all_ts else None
    r["newest_message"] = paris_dt(max(all_ts)) if all_ts else None
    r["thread_replies_cached_in_messages"] = replies
    r["thread_parents"] = parents
    r["thread_parent_reply_count_sum"] = reply_count_sum
    r["message_subtypes"] = dict(subtypes.most_common(10))
    r["sample_message_keys"] = sample
    # threads slice
    th = state.get("threads")
    if th is not None:
        found = {}; walk_msgs(th, None, found)
        r["threads_slice"] = {"type": type(th).__name__, "len": len(th) if hasattr(th, "__len__") else None,
                              "message_shaped_objects": len(found)}
    # any other slice holding message-shaped objects
    other = {}
    for k, v in state.items():
        if k in ("messages", "threads") or not isinstance(v, (dict, list)): continue
        found = {}; walk_msgs(v, None, found)
        if len(found) >= 3: other[k] = len(found)
    r["other_slices_with_message_shaped_objects"] = other
    # channel listing: per channel name + type + cached count (no DMs' user ids)
    rows = []
    for cid, c in chans.items():
        kind = "im" if c.get("is_im") else "mpim" if c.get("is_mpim") else "private" if (c.get("is_private") or c.get("is_group")) else "public"
        name = c.get("name") if kind in ("public", "private") else None
        rows.append({"id": cid, "name": name, "kind": kind,
                     "shared": bool(c.get("is_ext_shared") or c.get("is_shared") or c.get("is_org_shared")),
                     "cached_msgs": per_chan.get(cid, 0)})
    for cid in per_chan:
        if cid not in chans: rows.append({"id": cid, "name": None, "kind": "unknown(not in channels)", "cached_msgs": per_chan[cid]})
    rows.sort(key=lambda x: -x["cached_msgs"])
    r["channels"] = rows
    mems = {k: v for k, v in as_dict(state.get("members")).items() if isinstance(v, dict)}
    r["member_profiles"] = len(mems)
    mf = collections.Counter(); other_team = 0
    for m in mems.values():
        for f in FLAG_KEYS_MEM:
            if m.get(f) is True: mf[f] += 1
        if r["team_id"] and m.get("team_id") and m.get("team_id") != r["team_id"]: other_team += 1
    r["member_flags"] = dict(mf); r["members_from_other_teams"] = other_team
    me = mems.get(r["self_user_id"] or "", {})
    r["self_is_guest"] = {"is_restricted": me.get("is_restricted"), "is_ultra_restricted": me.get("is_ultra_restricted")} if me else "self profile not cached"
    return r

# ---------------------------------------------------------------- raw blob decode (fallback / diagnostics)
def decode_blob_file(path):
    import ccl_simplesnappy
    from ccl_chromium_reader.serialization_formats import ccl_v8_value_deserializer as v8d, ccl_blink_value_deserializer as bd
    raw = pathlib.Path(path).read_bytes()
    info = {"size": len(raw), "head": raw[:3].hex()}
    d = raw
    if raw[:3] == b"\xff\x11\x02":
        d = ccl_simplesnappy.decompress(io.BytesIO(raw[3:]))
        info["snappy"] = True
    info["decoded_head"] = d[:4].hex()
    off = None
    if len(d) > 16 and d[0] == 0xff and d[2] == 0xfe and d[15] == 0xff:
        off = 15
    elif len(d) > 4 and d[0] == 0xff and d[2] == 0xff:
        off = 2
    elif d[:1] == b"\xff":
        off = 0
    if off is None:
        info["error"] = "no V8 header"; return info, None
    p = bytearray(d[off:]); info["v8_version"] = p[1]
    if p[1] == 16: p[1] = 15
    val = v8d.Deserializer(io.BytesIO(bytes(p)), host_object_delegate=bd.BlinkV8Deserializer().read).read()
    return info, val

# ---------------------------------------------------------------- analyze
def analyze(snap_dir, report_path=None):
    snap = pathlib.Path(snap_dir)
    report = {"indexeddb": [], "redux_states": [], "blob_scan": [], "localstorage": {}, "root_state": {}, "service_worker": {}, "errors": []}
    from ccl_chromium_reader import ccl_chromium_indexeddb as idb

    # --- IndexedDB
    idb_root = snap / "IndexedDB"
    entries = sorted(idb_root.iterdir()) if idb_root.exists() else []
    report["indexeddb_dir_entries"] = [e.name for e in entries]
    def _is_sqlite(p):
        try:
            with open(p, "rb") as fh: return fh.read(16).startswith(b"SQLite format 3")
        except OSError: return False
    sqlite_like = [str(p.relative_to(idb_root)) for p in idb_root.rglob("*") if p.is_file() and _is_sqlite(p)]
    report["sqlite_files_in_indexeddb"] = sqlite_like[:50]
    for ldb in [e for e in entries if e.name.endswith(".indexeddb.leveldb")]:
        blob = idb_root / ldb.name.replace(".leveldb", ".blob")
        origin = ldb.name.replace(".indexeddb.leveldb", "")
        try:
            w = idb.WrappedIndexDB(ldb, blob if blob.exists() else None)
        except Exception as e:
            report["errors"].append(f"open {origin}: {e!r}"); continue
        for dbid in w.database_ids:
            try:
                db = w[dbid.dbid_no]
            except Exception as e:
                report["errors"].append(f"db {dbid}: {e!r}"); continue
            dbname = dbid.name
            entry = {"origin": origin, "db": dbname, "db_no": dbid.dbid_no, "stores": {}}
            report["indexeddb"].append(entry)
            is_redux = "redux" in (dbname or "").lower() or "persist" in (dbname or "").lower()
            for sname in db.object_store_names:
                st = {"records_total": 0, "decode_errors": 0, "keys": [], "value_types": collections.Counter()}
                entry["stores"][sname] = st
                errs = []
                latest = {}
                try:
                    for rec in db[sname].iterate_records(bad_deserializer_data_handler=lambda k, b: errs.append(1)):
                        st["records_total"] += 1
                        k = rec.key.value if rec.key is not None else None
                        ks = scrub(str(k))[:80]
                        prev = latest.get(ks)
                        if prev is None or (rec.is_live, rec.ldb_seq_no or 0) >= (prev.is_live, prev.ldb_seq_no or 0):
                            latest[ks] = rec
                except Exception as e:
                    report["errors"].append(f"iterate {dbname}/{sname}: {e!r}")
                st["decode_errors"] = len(errs)
                st["unique_keys"] = len(latest)
                st["keys"] = sorted(latest)[:30]
                for ks, rec in latest.items():
                    v = rec.value
                    st["value_types"][type(v).__name__] += 1
                st["value_types"] = dict(st["value_types"])
                if is_redux:
                    merged = {}
                    for ks, rec in latest.items():
                        v = rec.value
                        if isinstance(v, str) and v[:1] in "{[":
                            try: v = json.loads(v)
                            except Exception: pass
                        if isinstance(v, dict) and any(x in v for x in ("messages", "channels", "members", "bootData")):
                            res = analyze_state(v, f"{origin} :: {dbname} :: {sname} :: key={ks}")
                            res["record_is_live"] = rec.is_live
                            res["external_blob"] = str(rec.external_value_path) if getattr(rec, "external_value_path", None) else None
                            report["redux_states"].append(res)
                        else:
                            merged[ks] = v
                    if merged and any(x in merged for x in ("messages", "channels", "members")):
                        report["redux_states"].append(analyze_state(merged, f"{origin} :: {dbname} :: {sname} :: (per-slice records)"))
                    elif merged:
                        st["non_state_record_shapes"] = {k: shape(v) for k, v in list(merged.items())[:5]}
        # --- raw blob scan (diagnostic / fallback)
        if blob.exists():
            for bf in sorted(p for p in blob.rglob("*") if p.is_file()):
                rel = str(bf.relative_to(blob))
                try:
                    info, val = decode_blob_file(bf)
                    info["file"] = rel
                    if isinstance(val, dict):
                        info["top_keys"] = sorted(val.keys())[:40]
                        if not any(rel in (s.get("external_blob") or "") for s in report["redux_states"]) and \
                                any(x in val for x in ("messages", "channels")):
                            info["note"] = "state-like blob not reached via IDB records (orphan/old version?)"
                            info["summary"] = analyze_state(val, "raw blob " + rel)
                    else:
                        info["value_type"] = type(val).__name__
                except Exception as e:
                    info = {"file": rel, "error": f"{type(e).__name__}: {str(e)[:200]}"}
                report["blob_scan"].append(info)

    # --- Local Storage
    try:
        from ccl_chromium_reader import ccl_chromium_localstorage as ls
        lsdb = ls.LocalStoreDb(snap / "LocalStorage_leveldb")
        keys = collections.Counter(); msgish = {}; teams = {}
        for sk in lsdb.iter_storage_keys():
            for rec in lsdb.iter_records_for_storage_key(sk):
                if not rec.is_live: continue
                name = rec.script_key
                base = re.sub(r"T[A-Z0-9]{6,}|U[A-Z0-9]{6,}|E[A-Z0-9]{6,}", "<ID>", name)
                keys[f"{sk} :: {scrub(base)[:90]}"] += 1
                val = rec.value or ""
                if name == "localConfig_v2":
                    try:
                        cfg = json.loads(val)
                        for tid, t in (cfg.get("teams") or {}).items():
                            teams[tid] = {k: t.get(k) for k in ("name", "domain", "user_id", "enterprise_id", "enterprise_name") if k in t}
                        report["localstorage"]["localConfig_v2_top_keys"] = sorted(k for k in cfg.keys())
                    except Exception as e:
                        report["errors"].append(f"localConfig_v2 parse: {type(e).__name__}")
                    continue
                n = len(re.findall(r'"ts"\s*:\s*"\d{9,10}\.\d+"', val))
                if n and ('"text"' in val):
                    msgish[scrub(base)[:90]] = msgish.get(scrub(base)[:90], 0) + n
        report["localstorage"]["key_patterns"] = dict(keys.most_common(60))
        report["localstorage"]["message_bearing_keys(ts+text count)"] = msgish
        report["localstorage"]["teams_from_localConfig_v2(no tokens)"] = teams
    except Exception as e:
        report["errors"].append(f"localstorage: {type(e).__name__}: {e}")

    # --- root-state.json (workspace inventory only)
    try:
        rs = json.loads((snap / "root-state.json").read_text(encoding="utf-8"))
        report["root_state"]["top_keys"] = sorted(rs.keys())
        ws = rs.get("workspaces") or rs.get("teams") or {}
        report["root_state"]["workspaces"] = {k: {kk: v.get(kk) for kk in ("name", "domain", "id", "teamId") if isinstance(v, dict) and kk in v}
                                              for k, v in (ws.items() if isinstance(ws, dict) else [])}
    except Exception as e:
        report["errors"].append(f"root-state: {type(e).__name__}: {e}")

    # --- Service Worker CacheStorage (URLs path-only, no bodies)
    try:
        swr = snap / "SW_CacheStorage"
        if swr.exists():
            from ccl_chromium_reader import ccl_chromium_cache as cc
            summary = {}
            for idx in swr.rglob("index"):
                cdir = idx.parent
                try:
                    cache = cc.ChromiumSimpleFileCache(cdir)
                    pats = collections.Counter(); n = 0
                    for key in cache.keys():
                        n += 1
                        u = re.sub(r"^\d+/\d+/_dk_[^ ]+ ", "", str(key))
                        u = u.split("?")[0]
                        m = re.match(r"(https?://[^/]+)(/[^/]*)?(/[^/]*)?", u)
                        pats[scrub("".join(g or "" for g in (m.groups() if m else [u[:60]])))] += 1
                    summary[str(cdir.relative_to(swr))] = {"entries": n, "url_patterns": dict(pats.most_common(15))}
                except Exception as e:
                    summary[str(cdir.relative_to(swr))] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
            report["service_worker"]["caches"] = summary
            report["service_worker"]["files"] = sum(1 for p in swr.rglob("*") if p.is_file())
        else:
            report["service_worker"]["note"] = "no CacheStorage copied"
    except Exception as e:
        report["errors"].append(f"service worker: {type(e).__name__}: {e}")

    txt = scrub(json.dumps(report, indent=1, default=str, ensure_ascii=False))
    if report_path:
        pathlib.Path(report_path).write_text(txt, encoding="utf-8")
    return txt


def diag(snap_dir):
    """Explain why ccl's record iterator fails on Slack's redux records (no values printed)."""
    from ccl_chromium_reader import ccl_chromium_indexeddb as idb
    from ccl_chromium_reader.serialization_formats import ccl_v8_value_deserializer as v8d, ccl_blink_value_deserializer as bd
    snap = pathlib.Path(snap_dir); root = snap / "IndexedDB"
    for ldb in root.glob("*.indexeddb.leveldb"):
        blob = root / ldb.name.replace(".leveldb", ".blob")
        w = idb.WrappedIndexDB(ldb, blob if blob.exists() else None)
        raw = w._raw_db
        for dbid in w.database_ids:
            if "redux" not in (dbid.name or "").lower(): continue
            db = w[dbid.dbid_no]
            for sid, sname in enumerate(db.object_store_names, 1):
                prefix = idb.IndexedDb.make_prefix(dbid.dbid_no, sid, 1)
                res = collections.Counter(); keys = collections.Counter()
                for rec in raw._fetched_records:
                    if not rec.key.startswith(prefix): continue
                    k = idb.IdbKey(rec.key[len(prefix):])
                    ks = re.sub(r"U[A-Z0-9]{6,}", "U*", scrub(str(k.value)))[:70]
                    state = "live" if rec.state == idb.ccl_leveldb.KeyState.Live else "deleted"
                    if not rec.value:
                        res[(state, "empty value")] += 1; keys[(ks, state)] += 1; continue
                    try:
                        vv, vr = idb._le_varint_from_bytes(rec.value)
                        pre = raw.read_record_precursor(k, dbid.dbid_no, sid, rec.value[len(vr):], None)
                        if pre is None:
                            res[(state, "precursor None (blob info missing)")] += 1
                        else:
                            v = v8d.Deserializer(pre[1], host_object_delegate=bd.BlinkV8Deserializer().read).read()
                            res[(state, "OK " + type(v).__name__ + " blob=" + str(pre[3]))] += 1
                    except Exception as e:
                        res[(state, type(e).__name__ + ": " + scrub(str(e))[:120])] += 1
                    keys[(ks, state)] += 1
                out("DB", dbid.name, "store", sname)
                for (st, msg), n in sorted(res.items(), key=lambda x: -x[1])[:25]: out("  ", n, st, msg)
                out("  record keys (key, state): count")
                for (kk, st), n in keys.most_common(20): out("    ", kk, st, n)

if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "snapshot":
        snapshot(sys.argv[2], sys.argv[3])
    elif len(sys.argv) >= 3 and sys.argv[1] == "analyze":
        try:
            print(analyze(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None))
        except Exception:
            print(scrub(traceback.format_exc()))
    elif len(sys.argv) >= 3 and sys.argv[1] == "diag":
        diag(sys.argv[2])
    else:
        print(__doc__)
