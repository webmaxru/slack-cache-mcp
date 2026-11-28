# Architecture decisions

Record deviations or underspecifications from `docs/PLAN.md` here.

## 2026-11-18 — M0/M1 kickoff

### MCP SDK pin
Pinned `mcp==2.3.0` (current stable v2 line). Import path `from mcp.server import MCPServer` as documented.

### Fixture generation (M0)
Prefer a pure-Python blob builder (V8 subset → Blink v21 envelope → literal Snappy → `ff 11 02`) over requiring headless Chromium for CI. HTML PoC remains under `docs/reference/fixture_index.html` and `tests/fixture_gen/site/`. Node cross-check via `parse_blob.mjs` is optional (skip if Node absent).

### Package data dir in tests
Tests use a temp `--data-dir`; they never touch `%LOCALAPPDATA%\slackcache`.

### offline-only rule path
Copied `.cursor/offline-only.mdc` to `.cursor/rules/offline-only.mdc` to match PLAN layout while keeping the original path.

### urllib.Request audit event
CPython 3.12.11 on the Windows self-hosted worker does not emit a `urllib.Request` audit event when constructing `urllib.request.Request`. Netguard still blocks `http.client.connect`, `socket.connect`, `socket.getaddrinfo`, `webbrowser.open`, and non-allowlisted `subprocess.Popen`. Product code must not import `urllib.request` (import-linter + AST).

### Local uv index mirror
`files.pythonhosted.org` (Fastly wheel CDN) TLS handshake fails on Maxim's Windows corp laptop (`HandshakeFailure` / alert 40 for curl, OpenSSL, Python ssl, and uv). `pypi.org` simple API still works; `UV_NATIVE_TLS=1` does not help. Fix is **user-local** (not committed): User env `UV_INDEX_URL=https://packagefeedproxy.microsoft.io/pypi/simple/` and/or `%APPDATA%\uv\uv.toml` (same URL as `ProgramData\pip\pip.ini`). Optional gitignored project `uv.toml` may list that feed and Tsinghua as non-default extras. CI uses official PyPI. Ops note: Project store `docs/m6-laptop2-uv-tls.md`.

### import-linter forbidden modules
`import-linter` rejects external **subpackages** (e.g. `urllib.request`, `http.client`) as `forbidden_modules`. Those stay covered by `tests/test_no_network_imports.py` AST scan. The import-linter contract forbids top-level network client packages (`requests`, `httpx`, …).

## 2026-11-19 — M2 continuous sync

### Archive DB encryption (spike result)
Probed SQLCipher (`pysqlcipher3` / `sqlcipher3`) and APSW+sqlite3mc on this Windows worker: neither is installed, and PLAN requires sqlite-vec (M4) to load into the same build. **Decision: defer encrypting `archive.db`.** Rely on BitLocker + ACL/EFS on `%LOCALAPPDATA%\slackcache` for the DB; keep sanitized state vault as AES-256-GCM (via `cryptography`) with keyring/DPAPI (file fallback for tests). Revisit SQLCipher/sqlite3mc after M4 embeds sqlite-vec. Command: `slackcache encryption-spike`.

### Vault key storage
Prefer OS keyring (`keyring` → Windows Credential Manager). Write `vault.key` (hex, 0600) under the data dir only as a fallback (e.g., on first key creation and/or when keyring is unavailable) so tests and recovery can work without keyring. Tests use `--data-dir` temp paths and never touch the real `%LOCALAPPDATA%\slackcache`.

### Suspected deletion streak
PLAN §8.2: absent from a contiguous window in ≥2 consecutive *new* state generations → `deleted_state='suspected'`. Tracked in `suspected_streaks`. Eviction outside the window is ignored. Explicit tombstones still win as `deleted`.

### Daemon cadence defaults
Watcher (watchdog / ReadDirectoryChangesW) debounce 2 s; poll 60 s; full pass 900 s. Overridable via CLI flags. Single-instance: Windows mutex `Local\slackcache-daemon`; lockfile elsewhere.

### Task Scheduler / netguard
`schtasks /Create|/Delete|/Query` allowed only for task name `slackcache-daemon`; `/Create` requires `/XML`.

### PyInstaller
`packaging/slackcache.spec` builds onedir `slackcache.exe`. Firewall snippet: `packaging/firewall-block-outbound.ps1` (admin). Dev firewall on `uv python find` remains documented in README.

## 2026-11-20 — M3 maximum capture

### Synthetic side-cars for non-blob sources
Real Chromium LevelDB/Session Storage/HTTP cache encodings are exercised via ccl when present. For CI and unit tests we also accept JSON side-cars under the mirror (same pattern as M1 `localConfig_v2.json`):
- `IndexedDB/<db>/records.json` — canvas + historical IDB records
- `Session Storage/session_entries.json`
- `Local Storage/local_entries.json` — drafts (opt-in)
- `Cache/cache_entries.json` — HTTP cache metadata (opt-in)
- `Notifications/notifications_win.json` — Windows toast previews (opt-in)

Product still never reads the live Slack profile; tests use temp `--data-dir` / `--source` only.

### Opt-in defaults
`capture_drafts`, `capture_downloads`, `capture_http_cache`, `capture_notifications`, `capture_ocr` default **false**. CLI: `--capture-drafts` etc. on `snapshot --once`. OCR remains a no-op stub (no Tesseract/WinRT dependency); enable later without schema change (`files.ocr_text`).

### IdbBackend
`detect_idb_backend` / `open_idb_backend` select LevelDB (default) or SQLite when a `SQLite format 3` header appears under `IndexedDB/`. SQLite path always copy+WAL-replay on the copy, then `mode=ro&immutable=1`.

### Slack-export
Implemented from the public export directory layout (channels/groups/dms/mpims/users + per-channel day JSON). No slackdump/AGPL code. `slackcache export --format slack-export|jsonl`.

### Schema
`SCHEMA_VERSION=3` adds reactions, files, message_files, saved_items, canvases, canvas_versions, drafts, source_yield (additive migrations).

## 2026-11-25 — M4 intelligent search

### Embedding default
Production path: fastembed ONNX `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (384-d) after `slackcache models fetch` (HuggingFace only; no Slack). Runtime sets `HF_HUB_OFFLINE=1`. If model files are missing, fall back to deterministic `HashingEmbedder` (same dim) so hybrid search and tests stay offline.

### Package boundary for fastembed
`slack_cache_mcp.embeddings` is outside the offline import-linter source set (same idea as `sync/`): it may depend on fastembed/httpx transitively. `search/` and `mcp_server/` only use the `Embedder` protocol + `HashingEmbedder`. CLI/daemon inject the concrete backend.

### sqlite-vec
Load via `sqlite_vec.load(conn)` when `enable_load_extension` works. `docs_vec` is created at runtime (dimension from the active embedder). Always also write float32 BLOBs to `doc_embeddings` and use numpy cosine KNN when sqlite-vec cannot load (common on macOS Python builds without `enable_load_extension`).

### Eval harness
CI uses `HashingEmbedder` with planted synonym groups so paraphrase queries prove hybrid ≥ lexical + 10% relative on a synthetic labelled set. Latency bench defaults to a few thousand messages in CI; `slackcache eval --bench --bench-n 200000` is the PLAN laptop check.

### Operators
`with:`, `during:YYYY-MM`, `has:reaction`, `is:reply`, `is:edited` are implemented. Calendar dates interpret in Europe/Paris (`tzdata` dependency on Windows).

### Schema
`SCHEMA_VERSION=4` adds `search_docs` (+ runtime `docs_vec`).

## 2026-11-27 — M5 multi-device sync (local)

### Age encryption via pyrage
Bundle files use the age v1 X25519 recipient format through `pyrage` (Rust bindings). Signing is separate Ed25519 via `cryptography`. Envelope = zstd(JSONL change_log) + Ed25519 signature fields, then age-encrypted to all registered device age recipients (including self). Filename: `bundle-<device>-<fromseq>-<toseq>.jsonl.zst.age`.

### Device key storage
Ed25519 + age identity secrets prefer OS keyring (`slack-cache-mcp` / `device-{ed25519|age}-<id>`). File fallback `device.secrets` under the data dir (0600) for tests/recovery, mirroring vault key handling. Public halves live in `device.json` and `devices.public_key` (JSON `{"ed25519","age"}`).

### Peer bootstrap
Age encrypt requires recipients at export time. Peers exchange public identity via `slackcache sync show-device` + `sync peers add --from`. Import also refreshes the sender's public keys from the signed header after successful decrypt.

### Outbox / apply
`change_log` carries full sanitized payloads for `message_version`, `workspace`, `channel`, and `user`. Apply is idempotent on `(device_id, entity, entity_key, payload_sha256)` and re-sanitizes. `sync_cursors` tracks folder push/pull watermarks. `SCHEMA_VERSION=5`.

### Convergence hash
`state_hash` covers ordered projections of `messages`, `message_versions`, `channels`, `users`, and `workspaces` (not device-local drafts/paths). Property tests shuffle 2–4 devices and re-import duplicates.

### Bundle trust + envelope v2
Import rejects unknown senders and signing/age key mismatches against `sync peers add` registrations; header keys never overwrite a registered peer. Envelope format version 2 signs the canonical header+payload (not payload alone). Export runs `backfill_outbox` so upgraded M4 archives ship full sanitized facts. HLC watermark is persisted in `meta.hlc_watermark` and restored for ingest/import.

### Copilot round-2..5 hardening
- Device identity fails closed if `device.json` is missing keys or malformed; `device.secrets` only when keyring unconfirmed; create mode 0600.
- `sync peers add` resets **all** push cursors. Decrypt failures are always errors (remove superseded folder bundles after full re-export).
- Import rejects forged row authors and sighting payload `device_id` mismatches.
- Apply reprojects + refreshes search docs inside the same transaction; sightings merge times into version rows; version outbox stays free of sighting timestamps; epoch bounds are ignored as unknown.
- Directory upserts maintain `field_hlcs_json`. Unclocked values use sentinel `*`. Repair migration (policy `v7-*`) preserves real table clocks, restores dominating `(value, clock)` pairs only for unknown table clocks, bumps `directory_clock_epoch`, and rebuilds local directory outbox with `clock_epoch` + one-shot `clock_repair`. Receivers force-apply only facts that carry repair authority (higher epoch or `clock_repair`); lower-epoch clocks are null-fill only. Full re-exports write the new bundle before deleting older local files; incremental exports retain history. Bundle `from_seq`/`to_seq` use min/max sequence; import verifies all files then applies rows in global entity order with per-row failure isolation. Channel outbox emits legacy `aliases` plus `alias_records`; backfill repairs object-only rows.
- Watermark updates use `BEGIN IMMEDIATE` compare-and-swap across connections.

## 2026-11-27 — M5.1 Edge browser IndexedDB ingest

### Second offline source (Edge only)
Microsoft Edge Chromium `%LOCALAPPDATA%\Microsoft\Edge\User Data\Default` is auto-discovered when Slack’s `https_app.slack.com` IndexedDB is present (`kind=win-edge`). It feeds the **same** `archive.db` as desktop via existing `(channel_id, ts)` + `content_sha256` versioning and multi-source sightings. Distinct `source_id` (hash of device + path) and `sources.kind` distinguish Edge from desktop. **Chrome is out of scope for M5.1.**

### IndexedDB-only allowlist for browser sources
Edge is a full Chromium user-data tree (credential-rich). Snapshot mode `edge-idb` copies **only** `IndexedDB/https_app.slack.com_0.indexeddb.{blob,leveldb}` (skip `LOCK`). Never open or copy Cookies, Local State, Network, Login Data, passwords, or Edge Local/Session Storage. Desktop profiles keep the existing desktop allowlist.

### Sighting provenance
Edge-ingested observations use `edge.<slice>` source labels (e.g. `edge.redux.messages`) with the same `SOURCE_RANK` as the desktop slice, so one device can record both desktop and Edge sightings without collapsing them, while content still dedupes into one `message_versions` row.

### Motivation (Maxim A/B)
Offline probe of Edge IDB showed reply **bodies** for threads where the desktop archive had `reply_count` metadata but `replies_captured=0`. M5.1 productizes that second observation stream under the offline-only rules.

### Copilot hardening (PR #7)
- Edge overrides without Slack IDB never fall back to the desktop allowlist; kind inference resolves symlinks/junctions.
- After Edge ingest, raw IndexedDB is deleted from the mirror (sanitized content already in vault/archive).
- Edge never drives suspected-deletion; desktop coverage skips ts that already have `edge.*` sightings.
- Known state generations still record multi-source sightings (desktop vs Edge provenance).
- Doctor inventories Session/Local Storage / HTTP cache only on desktop profiles; Edge is Slack-IDB-only.
