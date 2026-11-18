# Kickoff: slack-cache-mcp (Cursor IDE agent + Cursor CLI)

This file has four parts:
1. One-time setup on your Windows PC (5 minutes).
2. **The kickoff prompt** (paste into Cursor Agent, or point the CLI at it).
3. Cursor CLI variants (interactive, plan-first, headless).
4. Your manual acceptance run on real data after M1, and follow-up prompts for M2-M5.

---

## 1. One-time setup (PowerShell)

```powershell
# 1) New private repo folder (do NOT put it under OneDrive if you can avoid it)
mkdir C:\src\slack-cache-mcp; cd C:\src\slack-cache-mcp
git init -b main

# 2) Unzip slack-offline-mcp-plan.zip somewhere, then copy the planning files in:
$plan = "$env:USERPROFILE\Downloads\slack-offline-mcp-plan"   # adjust
mkdir docs\reference, .cursor\rules -Force | Out-Null
Copy-Item "$plan\PLAN.md"                    docs\PLAN.md
Copy-Item "$plan\KICKOFF_PROMPT.md"          docs\KICKOFF_PROMPT.md
Copy-Item "$plan\reference\*"                docs\reference\
Copy-Item "$plan\cursor-rules\offline-only.mdc" .cursor\rules\offline-only.mdc
Copy-Item "$plan\cursor-rules\cli.json"      .cursor\cli.json
git add -A; git commit -m "docs: plan, reference decoder, cursor rules"

# 3) Tooling
winget install --id=astral-sh.uv -e        # if uv is missing
uv python install 3.12
# Node is only needed for dev-time synthetic fixture generation (optional): winget install OpenJS.NodeJS.LTS
```
Optional: create the private GitHub repo and push. Keep it private; it will never contain real data, but the code is about your workspaces.

The `.cursor/cli.json` file denies the agent read access to your real Slack profile and your future archive (`%AppData%\Slack`, `%LocalAppData%\Packages\*Slack*`, `%LocalAppData%\slackcache`), denies fetches to slack.com, and denies `curl`/`wget`/`Invoke-WebRequest`. The `.cursor/rules/offline-only.mdc` rule is always applied in both the IDE and the CLI.

---

## 2. The kickoff prompt

Paste everything inside the block into Cursor Agent (Agent mode; a strong reasoning model). For the CLI, see part 3.

```text
You are implementing "slack-cache-mcp" in this repository. Read these files first, completely, before writing code:
- docs/PLAN.md            (the architecture and milestone plan; it is the source of truth)
- .cursor/rules/offline-only.mdc  (hard rules; they override everything else)
- docs/reference/README.md and docs/reference/slack_cache_probe.py (a decoder already proven on the real Windows
  Microsoft Store Slack cache; behavioural reference, do not copy it in as-is)
- docs/reference/fixture_index.html (synthetic IndexedDB writer from the proof of concept)

## What we are building
An offline-only tool for Windows (also macOS/Linux) that snapshots the Slack desktop app's local cache, decodes it,
accumulates an append-only SQLite archive (never deleting what Slack evicts), and exposes it through an MCP server
with search. Stage 1 is local only; the storage layer must be behind a repository interface so stage 2 can add a
Postgres/pgvector backend and a remote MCP over Streamable HTTP with OAuth 2.1.

## Absolute rules (summary; full text in .cursor/rules/offline-only.mdc)
1. Product code never contacts Slack or any server. No HTTP/WebSocket clients, no Slack SDKs, no token/cookie handling.
   Only src/slack_cache_mcp/sync/ (stage 2, not now) may ever import an HTTP client.
2. Never read Network/, Cookies, Local State. Parse Local Storage localConfig_v2 only into allowlisted fields
   (id, name, domain, url, user_id, enterprise_id, enterprise_name); drop everything else, including tokens.
   Everything decoded passes through desktop/sanitize.py before it is persisted, logged, or exported.
3. Never drive, launch, inject into, or write to Slack or its profile directory. Copy files first (skip LOCK), parse copies.
4. Use synthetic data only. Do NOT read my real Slack profile or %LOCALAPPDATA%\slackcache. I run real-data checks myself.
5. Never weaken a guard to make a test pass.

## Stack (do not change without updating docs/PLAN.md and telling me why)
Python 3.12 + uv; ruff; mypy (strict for desktop/ and ingest/); pytest + pytest-socket + hypothesis; import-linter;
typer for the CLI; the official MCP Python SDK v2.x (`from mcp.server import MCPServer`; FastMCP was renamed in v2);
SQLite with FTS5 (sqlite-vec and embeddings come in M4, not now); vendored ccl_chromium_reader 0.3.19
(commit 7a01b07dc5205a9c12004f2c9eabce0bb4697860) and ccl_simplesnappy 0.4 (commit 3d085230baa8c46cf2090ebba29bf6e8eab31087),
both MIT, under src/slack_cache_mcp/_vendor/ with their licences and a THIRD_PARTY_NOTICES.md entry.
Before vendoring, check the current MCP SDK and sqlite/FTS5 APIs against their docs; pin versions in pyproject.

## Proven decoder facts (from the real cache; design around them)
- Profile roots: Windows Store `%LOCALAPPDATA%\Packages\91750D7E.Slack_8she8kybcnzg4\LocalCache\Roaming\Slack`
  (also glob `%LOCALAPPDATA%\Packages\*Slack*\LocalCache\Roaming\Slack`), Windows direct `%APPDATA%\Slack`,
  macOS `~/Library/Application Support/Slack` and `~/Library/Containers/com.tinyspeck.slackmacgap/Data/Library/Application Support/Slack`,
  Linux `$XDG_CONFIG_HOME/Slack`, `~/.config/Slack`, Flatpak `~/.var/app/com.slack.Slack/config/Slack`, Snap `~/snap/slack/current/.config/Slack`.
  On Windows never rely on HOME; use LOCALAPPDATA/APPDATA (with SHGetKnownFolderPath fallback).
- Persisted Redux state = one blob file per signed-in workspace/account under
  `IndexedDB\https_app.slack.com_0.indexeddb.blob\<db>\<xx>\<n>`. Format: `ff 11 02` + raw (unframed) Snappy ->
  Blink envelope `ff 15 fe ...` (Blink v21) -> V8 payload `ff 10` (wire v16) at offset 15. Older layouts: V8 at offset 2
  (`ff <blink> ff <v8>`) or 0. Relabel V8 version 16 -> 15 and deserialize with ccl's V8 deserializer +
  BlinkV8Deserializer host-object delegate. Locate the V8 header robustly by scanning the first 64 bytes.
- IDB DB `reduxPersistence`, store `reduxPersistenceStore`, key `persist:slack-client-<ID>-<USER>` where ID is a team
  `T...` OR an enterprise `E...` (Enterprise Grid). ccl's `iterate_records` FAILS on this record (174 decode errors on the
  real cache), so M1 reads blob files directly and identifies the workspace from the decoded state
  (`selfTeamIds.teamId`/`defaultWorkspaceId`, `bootData.team_id`, `bootData.user_id`, `teams`). Values may be wrapped
  `ff 11 01` (replaced by blob reference) or `ff 11 02` (Snappy); implement `idb_values.unwrap()` for all branches with unit
  tests even if M1 only uses the blob path.
- State top-level slices include channels, members, messages{channel_id:{ts:message}}, threads, threadSub, allThreads,
  channelHistory, channelCursors, channelLatests, files, saved, reactions, pins, lists, searchResults, bots, teams,
  bootData, selfTeamIds (100+ keys). M1 maps workspaces, channels, members, messages (incl. replies found in messages/threads).
- Message objects contain client-private keys (`__meta__`, `_hidden_reply`, `_rxn_key`, `_last_read`, `_subscribed`) that
  must be excluded from the content hash, along with volatile thread stats (reply_count, reply_users, reply_users_count,
  latest_reply) and reactions (stored separately).
- Copying while Slack runs works with plain reads if you skip `LOCK` and retry PermissionError/OSError with backoff;
  blob files can vanish or be mid-rewrite (copy only when size is stable; validate by decoding).
- A `canvas-<U>-<T>` IndexedDB DB exists (syncer.Document/Section records; ccl decodes it) - M3, not now.
- Local Storage has localConfig_v2 (tokens!), persist-v1::<T>::<U>::drafts etc. root-state.json lives at storage/root-state.json.

## Your task now: Milestone M0, then Milestone M1 (see docs/PLAN.md section 15 for acceptance criteria)

M0 - bootstrap and guardrails
1. pyproject (uv), src layout exactly as docs/PLAN.md section 3, console script `slackcache`.
2. src/slack_cache_mcp/netguard.py: sys.addaudithook that blocks non-loopback socket.connect/sendto/getaddrinfo,
   urllib.Request, http.client connect, webbrowser.open, and subprocess.Popen outside an allowlist. Installed at the start of
   every CLI entry point. Loopback must stay allowed (asyncio on Windows uses a loopback socketpair).
3. .importlinter contracts + tests/test_no_network_imports.py (AST scan of src/ excluding sync/ for forbidden modules and
   for string literals matching slack.com|slack-edge.com|slack-msgs.com|xox[a-z]-), pytest-socket configured with
   --disable-socket --allow-hosts=127.0.0.1,::1.
4. Vendor ccl (pinned commits above) with licences; stub brotli/zstd like the probe; allowlist ccl's urllib.parse import.
5. tests/fixture_gen/: synthetic fixture generator. Preferred: a pure-Python builder that writes blob files in the exact
   on-disk format (V8 serialization of plain JSON-like values is simple to implement for the subset we need: objects,
   arrays, strings (one-byte and two-byte), integers, doubles, booleans, null/undefined), wrapped in the Blink v21 envelope,
   Snappy-compressed (write a tiny literal-only Snappy encoder or vendor one), and prefixed `ff 11 02`; plus a minimal
   LevelDB/IndexedDB directory is optional for M1. Cross-check the builder against docs/reference/parse_blob.mjs and the
   probe's decode_blob_file() logic in a test (Node optional: skip if absent). Generate at least: 3 workspaces (one with
   an E... key and a guest self-user), public/private/ext-shared/IM/MPIM channels, threads with replies, an edited message,
   a tombstone, file metadata, and FAKE tokens planted in bootData and in a fake localConfig_v2 JSON.
   Also build a fake Local Storage source for localConfig_v2 (a JSON fixture fed through the same parse function is fine for M1).
6. CI: GitHub Actions matrix windows-latest, ubuntu-latest, macos-latest; ruff, mypy, pytest, lint-imports, gitleaks.
7. Commit after each step (conventional commits).

M1 - offline desktop source + archive + MCP stdio (Windows first)
1. desktop/locate.py with tests using a fake filesystem/env (Store, direct, both = split-brain warning, macOS, Linux, override).
2. desktop/snapshot.py: allowlist copy (IndexedDB/**, Local Storage/leveldb/**, storage/root-state.json, local-settings.json),
   skip LOCK, retries, size-stable blobs, manifest with sha256, incremental mirror keyed by (size, mtime_ns), never deletes
   source files, never opens source files for writing (test this).
3. desktop/chromium/idb_values.py + blob decoder; desktop/slack/redux.py extractors -> domain dataclasses;
   desktop/slack/localstate.py (team names only), desktop/slack/rootstate.py; desktop/sanitize.py (+ Hypothesis test that
   planted secrets never survive).
4. store/repository.py (ArchiveRepository Protocol) and store/sqlite/ with the section 7 tables needed for M1:
   devices, sources, snapshots, state_generations, workspaces, channels, channel_workspaces, channel_aliases, users,
   user_workspaces, message_versions, message_sightings, messages, threads, coverage_ranges (basic), channel_status,
   change_log, meta, messages_fts, messages_trigram. Migrations via meta.schema_version.
5. ingest/: content hashing (section 6.4), sparse-merge upserts, versions + sightings, deterministic projection (8.3),
   explicit tombstones, HLC + change_log rows written in the same transaction (outbox), idempotent re-ingest.
6. search/: query parser for Slack operators (in:, from:, before:, after:, on:, during:, has:, is:), lexical modes
   (auto, all, any, phrase, exact via trigram), BM25 ranking, filters, thread collapse, context expansion.
7. mcp_server/: MCPServer over stdio with tools list_workspaces, list_channels, read_channel, get_thread, get_context,
   get_message (versions flag), search_messages (hybrid mode returns lexical results for now and says so), resolve,
   get_user, cache_status (basic). Schemas as in docs/PLAN.md section 11; readOnlyHint=true; citations slk://...;
   logging to stderr only. Integration test that spawns the server over stdio with the SDK client and calls every tool
   against the synthetic fixture archive.
8. cli.py: locate, doctor (no network; includes IndexedDB backend detection, Blink/V8 version histogram, unknown slices,
   guard status), snapshot --once, search, mcp, status.
9. README: Windows quick start, Cursor MCP config snippet, firewall rule snippet, what is and is not captured.

## Working rules
- Small commits; run `uv run ruff check`, `uv run mypy`, `uv run lint-imports`, `uv run pytest` before each commit.
- Windows correctness first (paths, sharing violations, CRLF, long paths). Use pathlib everywhere.
- If something in docs/PLAN.md is wrong or underspecified, make the smallest sensible decision, record it in
  docs/DECISIONS.md, and continue. Ask me only if a decision would break a hard rule or change the architecture.
- Do not start M2+ features. Do not add embeddings, cloud, sync networking, file downloads, or UI automation.
- When done, reply with: what was built (by module), test results per OS (or local run), how to run the manual
  acceptance on my real cache (exact PowerShell commands), known gaps, and suggested next steps for M2.
```

---

## 3. Cursor CLI variants

The CLI binary is `agent` (installed by `irm 'https://cursor.com/install?win32=true' | iex` on Windows). Checked against Cursor CLI docs on 2026-11-18: `--plan`/`--mode=plan`, `-p/--print`, `--output-format text|json|stream-json`, `--force`/`--yolo`, `--trust` (headless), `--workspace`, `-w/--worktree`, `--model`, `--continue`/`--resume`. Project permissions live in `.cursor/cli.json`.

**Recommended: plan first, then build interactively** (you approve shell commands as they come):
```powershell
cd C:\src\slack-cache-mcp
agent --plan "Read docs/KICKOFF_PROMPT.md (part 2 is your task), docs/PLAN.md and .cursor/rules/offline-only.mdc. Propose a step-by-step implementation plan for M0 and M1 with the commits you will make. Do not write code yet."
# review, then in the same session switch to agent mode (Shift+Tab) or:
agent --continue "Plan approved. Execute M0, then M1, exactly as in docs/KICKOFF_PROMPT.md part 2. Commit after each step."
```

**One-liner (interactive agent mode):**
```powershell
agent "Execute the kickoff prompt in docs/KICKOFF_PROMPT.md part 2 (M0 then M1). Follow docs/PLAN.md and .cursor/rules/offline-only.mdc strictly."
```

**Headless, in an isolated worktree** (good for an unattended run; allowed commands come from `.cursor/cli.json`, everything else is rejected; avoid `--force` so the deny rules and approvals keep working):
```powershell
agent -p --trust -w m0-m1 --output-format text "Execute the kickoff prompt in docs/KICKOFF_PROMPT.md part 2 (M0 then M1). Follow docs/PLAN.md and .cursor/rules/offline-only.mdc strictly. Finish with the report described at the end of the prompt." | Tee-Object -FilePath .\agent-m0-m1.log
```
Stream progress instead: add `--output-format stream-json --stream-partial-output`.

**IDE:** open the folder in Cursor, confirm the rule `offline-only` shows as "Always", open Agent, paste part 2.

---

## 4. After M1: your manual acceptance run (real data, by you, not the agent)

```powershell
cd C:\src\slack-cache-mcp
uv run slackcache locate                 # should show the Store profile (redacted path)
uv run slackcache doctor                 # backend=LevelDB, blob V8 versions, decode health, guard active
uv run slackcache snapshot --once        # first ingest into %LOCALAPPDATA%\slackcache\archive.db
uv run slackcache status                 # counts per workspace; compare with the probe run
uv run slackcache snapshot --once        # again: should add nothing new except last_seen
uv run slackcache search "some topic"    # sanity check

# Prove the no-network property with Windows Firewall (requires an admin PowerShell, once):
$py = (uv python find 3.12)               # or the frozen exe after M2
New-NetFirewallRule -DisplayName "slackcache core: block outbound (dev)" -Direction Outbound -Action Block -Program $py
uv run slackcache snapshot --once        # must still work
```
Cursor MCP config (`%USERPROFILE%\.cursor\mcp.json`):
```json
{ "mcpServers": { "slack-cache": { "command": "uv", "args": ["run", "--directory", "C:\\src\\slack-cache-mcp", "slackcache", "mcp"] } } }
```
Note: blocking the shared uv Python also blocks other tools using that interpreter; M2 replaces this with a frozen `slackcache.exe` and a rule on that exe only.

Report back to the agent only counts and error categories, never message text.

### Follow-up prompts
- **M2:** "Implement Milestone M2 from docs/PLAN.md (continuous daemon with watcher + 60 s poll + 15 min full pass, incremental mirror, encrypted vault of sanitized states with retention, Task Scheduler install/uninstall, logs without content, backups, edit + explicit/suspected deletion, coverage ranges and channel staleness, archive-encryption spike with a written decision, PyInstaller onedir build + firewall docs). Keep all hard rules. Report the soak-test procedure for me to run."
- **M3:** "Implement Milestone M3 from docs/PLAN.md (all Redux slices, idb_values.unwrap for every IDB record incl. deleted LevelDB records, canvas DB, drafts opt-in, Session Storage scan, root-state downloads + opt-in local file text, opt-in HTTP-cache thumbnails, opt-in Windows notification DB source, IdbBackend abstraction + SQLite backend detector, Slack-export writer). Synthetic fixtures for each source. Add per-source yield to doctor."
- **M4:** "Implement Milestone M4 from docs/PLAN.md (fastembed multilingual embedder chosen by the eval harness, search_docs builder, sqlite-vec with partition/metadata filters, hybrid RRF + thread collapse + MMR + context expansion, remaining MCP tools, prompts/resources, eval harness with thresholds). Model download only via `slackcache models fetch`; runtime fully offline under netguard."
- **M5:** "Implement Milestone M5 from docs/PLAN.md (device identity + keyring keys, HLC, outbox change_log, deterministic rebuild, age-encrypted signed bundle export/import, convergence property tests)."
- **M6 (stage 2):** start with `agent --plan` and docs/PLAN.md sections 9.4 and 12.3; decide hosting/IdP and protection mode A/B first.
