# slack-cache-mcp

Offline-only archive and MCP server for the Slack **desktop app local cache**.

It never contacts Slack servers, never reads tokens/cookies, never drives or writes into the Slack app.
It copies allowlisted profile files, decodes them, accumulates an append-only SQLite archive, and serves them over MCP stdio.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Windows (primary), macOS, or Linux

## Quick start (Windows / synthetic fixtures)

```powershell
cd C:\path\to\slack-cache-mcp
uv sync
uv run slackcache locate
uv run slackcache doctor
uv run slackcache snapshot --once --source tests\fixtures\synthetic\profile --data-dir $env:TEMP\slackcache-dev
uv run slackcache search "synthetic" --data-dir $env:TEMP\slackcache-dev
uv run slackcache mcp --data-dir $env:TEMP\slackcache-dev
```

## Cursor MCP config (dev)

```json
{
  "mcpServers": {
    "slack-cache": {
      "command": "uv",
      "args": ["run", "--directory", "C:\\path\\to\\slack-cache-mcp", "slackcache", "mcp"]
    }
  }
}
```

## Firewall (optional, admin PowerShell)

```powershell
$py = (uv python find 3.12)
New-NetFirewallRule -DisplayName "slackcache core: block outbound (dev)" `
  -Direction Outbound -Action Block -Program $py
```

## What is captured (M1)

- Redux persisted states from IndexedDB blobs (channels, members, messages/threads)
- Workspace names from `localConfig_v2` (tokens dropped) and `storage/root-state.json`
- Append-only message versions + sightings; FTS5/trigram lexical search
- MCP tools: list/search/read/get/resolve/cache_status

## What is NOT captured

- Nothing from Slack servers (no API, Edge, file downloads, avatars)
- Tokens, cookies, `Network/`, `Local State`, `Login Data`
- Live LevelDB/SQLite opened in-place (copies only; `LOCK` skipped)

## Manual acceptance on a real cache (you run this; agents must not)

```powershell
uv run slackcache locate
uv run slackcache doctor
uv run slackcache snapshot --once
uv run slackcache status
uv run slackcache snapshot --once   # idempotent: last_seen only
uv run slackcache search "topic"
```

Report counts and error categories only — never message text or tokens.

## License

MIT. Third-party notices: `THIRD_PARTY_NOTICES.md`.
