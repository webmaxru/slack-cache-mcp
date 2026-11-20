# Packaging and firewall (M2)

## PyInstaller onedir

```powershell
uv sync
uv run pyinstaller packaging/slackcache.spec
# Output: dist/slackcache/slackcache.exe
Copy-Item -Recurse dist\slackcache\* "$env:LOCALAPPDATA\slackcache\bin\"
```

Point Cursor MCP at:

```json
{
  "mcpServers": {
    "slack-cache": {
      "command": "C:\\Users\\<you>\\AppData\\Local\\slackcache\\bin\\slackcache.exe",
      "args": ["mcp"]
    }
  }
}
```

## Outbound firewall (admin)

```powershell
# Frozen core binary (recommended)
.\packaging\firewall-block-outbound.ps1

# Or manually:
New-NetFirewallRule -DisplayName "slackcache core: block outbound" `
  -Direction Outbound -Action Block `
  -Program "$env:LOCALAPPDATA\slackcache\bin\slackcache.exe" -Profile Any
```

Defence in depth still includes `netguard.py`, import-linter, AST scan, and pytest-socket. The firewall rule is an OS-level belt for the frozen exe (venv `python.exe` is a redirector — do not rely on blocking only that).

## Task Scheduler

```powershell
uv run slackcache task install --data-dir $env:LOCALAPPDATA\slackcache
uv run slackcache task status
uv run slackcache task uninstall
```

Task action: `slackcache daemon --ensure-single` at logon with 5-minute repetition (IgnoreNew if already running).

## Archive encryption

See `docs/DECISIONS.md` (M2 spike): archive.db stays plaintext under BitLocker/ACL; vault objects are AES-GCM. Run `slackcache encryption-spike` to re-probe wheels.
