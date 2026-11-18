"""Typer CLI entry points. Installs netguard before any other work."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from slack_cache_mcp import netguard

app = typer.Typer(
    name="slackcache",
    help="Offline Slack desktop cache archive and MCP server.",
    add_completion=False,
    no_args_is_help=True,
)


def _boot() -> None:
    netguard.install()


@app.command("locate")
def locate_cmd(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """Show discovered Slack profile directories (redacted paths)."""
    _boot()
    from slack_cache_mcp.desktop.locate import discover_profiles, redact_path

    profiles = discover_profiles()
    rows = [
        {
            "kind": p.kind,
            "path": redact_path(p.path),
            "valid": p.valid,
            "primary": p.primary,
        }
        for p in profiles
    ]
    if json_out:
        typer.echo(json.dumps(rows, indent=2))
    else:
        if not rows:
            typer.echo("No Slack profiles found.")
            raise typer.Exit(1)
        for r in rows:
            flag = " [primary]" if r["primary"] else ""
            typer.echo(f"{r['kind']}: {r['path']}{flag}")


@app.command("doctor")
def doctor_cmd(
    json_out: bool = typer.Option(False, "--json"),
    data_dir: Path | None = typer.Option(None, "--data-dir"),
    source: Path | None = typer.Option(None, "--source"),
) -> None:
    """Offline health check (profiles, decode stats, guard status)."""
    _boot()
    from slack_cache_mcp.config import load_settings
    from slack_cache_mcp.desktop.doctor import run_doctor

    settings = load_settings(data_dir=data_dir, source=source)
    report = run_doctor(settings)
    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
    else:
        for k, v in report.items():
            typer.echo(f"{k}: {v}")
    if not report.get("netguard_installed"):
        raise typer.Exit(2)


@app.command("snapshot")
def snapshot_cmd(
    once: bool = typer.Option(False, "--once", help="One copy+decode+ingest cycle"),
    data_dir: Path | None = typer.Option(None, "--data-dir"),
    source: Path | None = typer.Option(None, "--source"),
) -> None:
    """Copy allowlisted cache files, decode, and ingest into the archive."""
    _boot()
    if not once:
        typer.echo("Use --once in M1 (daemon arrives in M2).", err=True)
        raise typer.Exit(2)
    from slack_cache_mcp.config import load_settings
    from slack_cache_mcp.pipeline import run_snapshot_once

    settings = load_settings(data_dir=data_dir, source=source)
    result = run_snapshot_once(settings)
    typer.echo(json.dumps(result, indent=2, default=str))


@app.command("search")
def search_cmd(
    query: str = typer.Argument(..., help="Search query (Slack operators supported)"),
    data_dir: Path | None = typer.Option(None, "--data-dir"),
    mode: str = typer.Option("lexical", "--mode"),
    limit: int = typer.Option(10, "--limit", min=1, max=50),
) -> None:
    """Search the local archive (same engine as MCP)."""
    _boot()
    from slack_cache_mcp.config import load_settings
    from slack_cache_mcp.search.service import SearchService
    from slack_cache_mcp.store.sqlite.repo import SqliteRepository

    settings = load_settings(data_dir=data_dir)
    repo = SqliteRepository(settings.archive_db, read_only=True)
    svc = SearchService(repo)
    hits = svc.search_messages(query=query, mode=mode, limit=limit)
    typer.echo(json.dumps(hits, indent=2, default=str))


@app.command("status")
def status_cmd(
    data_dir: Path | None = typer.Option(None, "--data-dir"),
    workspace: str | None = typer.Option(None, "--workspace"),
) -> None:
    """Show cache_status in the terminal."""
    _boot()
    from slack_cache_mcp.config import load_settings
    from slack_cache_mcp.store.sqlite.repo import SqliteRepository

    settings = load_settings(data_dir=data_dir)
    repo = SqliteRepository(settings.archive_db, read_only=True)
    typer.echo(json.dumps(repo.cache_status(workspace=workspace), indent=2, default=str))


@app.command("mcp")
def mcp_cmd(
    data_dir: Path | None = typer.Option(None, "--data-dir"),
    transport: str = typer.Option("stdio", "--transport"),
) -> None:
    """Run the MCP server (stdio)."""
    _boot()
    if transport != "stdio":
        typer.echo("M1 only supports --transport stdio", err=True)
        raise typer.Exit(2)
    from slack_cache_mcp.config import load_settings
    from slack_cache_mcp.mcp_server.server import run_stdio

    settings = load_settings(data_dir=data_dir)
    run_stdio(settings)


def main() -> None:
    # Ensure guard even if Typer callback path changes.
    netguard.install()
    app()


if __name__ == "__main__":
    main()
    sys.exit(0)
