# Reference material (no real data)

| File | What it is |
|---|---|
| `slack_cache_probe.py` | The offline, read-only probe that successfully decoded the real Microsoft Store Slack cache on Windows (Nov 2026). Contains no data, no tokens, no IDs. Behavioural reference for M1: `snapshot()` (allowlist copy, LOCK skip, retry on sharing violations), `decode_blob_file()` (`ff 11 02` + raw Snappy -> Blink envelope `ff 15 fe ...` -> V8 `ff 10` at offset 15, relabel v16 -> v15, ccl V8 deserializer with Blink host-object delegate), `analyze_state()` (slices/flags/counts), Local Storage key-pattern scan with token scrubbing, `diag()` (why ccl's record iterator fails on the Redux record). Needs vendored `ccl_chromium_reader` 0.3.19 + `ccl_simplesnappy` 0.4 (MIT) in `vendor/`. |
| `fixture_index.html` | Synthetic, Slack-shaped Redux state writer used in the original PoC (headless Chrome on 127.0.0.1 writes IndexedDB `reduxPersistence`/`reduxPersistenceStore` with 3000 fake messages + one small inline record). Starting point for `tests/fixture_gen/`. Extend it with multiple workspaces (incl. an `E...` enterprise key, key format `persist:slack-client-<ID>-<USER>`), threads, edits, tombstones, files, saved, a `canvas-<U>-<T>` DB, and planted FAKE tokens to test the sanitizer. |
| `parse_blob.mjs` | 10-line Node decoder (snappyjs + `v8.deserialize`) used to cross-check the blob format. Dev/diagnostics only; the product does not need Node. |

Facts observed on the real cache (structure only):
- IndexedDB dir contains only `https_app.slack.com_0.indexeddb.leveldb` and `.blob`; no SQLite files yet.
- DB `reduxPersistence`, store `reduxPersistenceStore`, one live key per account `persist:slack-client-<T|E id>-<U id>`; ccl's iterator: 1 record, 174 decode errors.
- One blob per signed-in workspace with a loaded state (3 of 4 workspaces), 150-450 KB compressed, Blink v21, V8 v16.
- Message objects include client-private keys (`__meta__`, `_hidden_reply`, `_rxn_key`, `_last_read`, `_subscribed`) besides Slack fields (`ts`, `text`, `user`, `blocks`, `attachments`, `files`, `edited`, `thread_ts`, `reply_count`, `replies`, `latest_reply`, `reply_users`, `subtype`, `client_msg_id`, `whisper_*`).
- A `canvas-<U>-<T>` IndexedDB database with `syncer.Document/*`, `syncer.Section/*`, `syncer.LoadData.Response.Index/Section/*` records decoded cleanly by ccl.
- Local Storage: `localConfig_v2` (tokens; extract names only), `persist-v1::<T>::<U>::drafts|draftQuotes|draftPendingDestinations|...`, `minChannelUpdated_<T>_<U>`, many `TOGGLE_STORAGE-*` flags. No message-bearing Local Storage keys found.
- `storage/root-state.json` top keys: `_persist, appTeams, bookmarks, diagnostics, downloads, settings, teams, webapp, workspaces, workspacesMeta`.
- Service Worker CacheStorage: static assets only.

Pinned upstream commits that worked on the real cache (vendor these, MIT):
- `ccl_chromium_reader` 0.3.19: https://github.com/cclgroupltd/ccl_chromium_reader @ `7a01b07dc5205a9c12004f2c9eabce0bb4697860`
- `ccl_simplesnappy` 0.4: https://github.com/cclgroupltd/ccl_simplesnappy @ `3d085230baa8c46cf2090ebba29bf6e8eab31087`
ccl's package `__init__` imports `brotli`/`zstd` for HTTP-cache bodies; stub them if absent (see top of the probe). Its `common.py` imports `urllib.parse` (string parsing only); allowlist that one module in the network-import check.
