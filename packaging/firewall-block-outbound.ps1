# Requires admin. Blocks outbound network for the frozen slackcache core binary.
# PLAN §12.1 — core process must not contact Slack (or any) servers.
#
# Usage (elevated PowerShell):
#   .\packaging\firewall-block-outbound.ps1
#   .\packaging\firewall-block-outbound.ps1 -Program "$env:LOCALAPPDATA\slackcache\bin\slackcache.exe"

param(
    [string]$Program = "$env:LOCALAPPDATA\slackcache\bin\slackcache.exe",
    [string]$DisplayName = "slackcache core: block outbound"
)

if (-not (Test-Path -LiteralPath $Program)) {
    Write-Error "Program not found: $Program. Build/install the PyInstaller onedir first."
    exit 1
}

# Remove prior rule with same display name (idempotent reinstall)
Get-NetFirewallRule -DisplayName $DisplayName -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule -ErrorAction SilentlyContinue

New-NetFirewallRule -DisplayName $DisplayName `
    -Direction Outbound `
    -Action Block `
    -Program $Program `
    -Profile Any |
    Out-Null

Write-Host "Created outbound block rule for: $Program"
Write-Host "Verify with: slackcache doctor"
Write-Host "Optional self-test (expects refusal): slackcache doctor --json"
