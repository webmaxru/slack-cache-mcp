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
