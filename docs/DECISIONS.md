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
`files.pythonhosted.org` TLS handshake fails on this Windows worker; `uv.toml` defaults the index to Tsinghua for local sync. GitHub Actions CI uses the default PyPI CDN.

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
