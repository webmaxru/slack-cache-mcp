"""Typer CLI entry points. Installs netguard before any other work."""

from __future__ import annotations

import contextlib
import json
import os
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

task_app = typer.Typer(help="Windows Task Scheduler integration", no_args_is_help=True)
app.add_typer(task_app, name="task")


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
    capture_drafts: bool = typer.Option(False, "--capture-drafts"),
    capture_downloads: bool = typer.Option(False, "--capture-downloads"),
    capture_http_cache: bool = typer.Option(False, "--capture-http-cache"),
    capture_notifications: bool = typer.Option(False, "--capture-notifications"),
) -> None:
    """Copy allowlisted cache files, decode, and ingest into the archive."""
    _boot()
    if not once:
        typer.echo("Use --once, or run `slackcache daemon` for continuous sync.", err=True)
        raise typer.Exit(2)
    from slack_cache_mcp.config import load_settings
    from slack_cache_mcp.pipeline import run_snapshot_once

    settings = load_settings(data_dir=data_dir, source=source)
    settings.capture_drafts = capture_drafts
    settings.capture_downloads = capture_downloads
    settings.capture_http_cache = capture_http_cache
    settings.capture_notifications = capture_notifications
    result = run_snapshot_once(settings)
    typer.echo(json.dumps(result, indent=2, default=str))


@app.command("daemon")
def daemon_cmd(
    data_dir: Path | None = typer.Option(None, "--data-dir"),
    source: Path | None = typer.Option(None, "--source"),
    ensure_single: bool = typer.Option(True, "--ensure-single/--no-ensure-single"),
    max_cycles: int | None = typer.Option(
        None, "--max-cycles", help="Stop after N cycles (tests)"
    ),
    poll_interval: float | None = typer.Option(None, "--poll-interval"),
    full_interval: float | None = typer.Option(None, "--full-interval"),
) -> None:
    """Long-running watcher + poll + full-pass continuous sync engine."""
    _boot()
    from slack_cache_mcp.config import load_settings
    from slack_cache_mcp.daemon import run_daemon

    settings = load_settings(data_dir=data_dir, source=source)
    result = run_daemon(
        settings,
        ensure_single=ensure_single,
        max_cycles=max_cycles,
        poll_interval_s=poll_interval if poll_interval is not None else settings.poll_interval_s,
        full_pass_interval_s=(
            full_interval if full_interval is not None else settings.full_pass_interval_s
        ),
    )
    typer.echo(json.dumps(result, indent=2, default=str))
    if not result.get("ok"):
        raise typer.Exit(1)


@app.command("backup")
def backup_cmd(
    data_dir: Path | None = typer.Option(None, "--data-dir"),
) -> None:
    """Backup archive.db via SQLite backup API into backups/."""
    _boot()
    from slack_cache_mcp.backup import backup_archive
    from slack_cache_mcp.config import load_settings

    settings = load_settings(data_dir=data_dir)
    result = backup_archive(settings.archive_db, settings.backups_dir)
    typer.echo(json.dumps(result, indent=2, default=str))
    if not result.get("ok"):
        raise typer.Exit(1)


@app.command("encryption-spike")
def encryption_spike_cmd() -> None:
    """Probe SQLCipher/sqlite3mc availability; print M2 decision (no network)."""
    _boot()
    from slack_cache_mcp.crypto_spike import run_encryption_spike

    typer.echo(json.dumps(run_encryption_spike(), indent=2, default=str))


@app.command("search")
def search_cmd(
    query: str = typer.Argument(..., help="Search query (Slack operators supported)"),
    data_dir: Path | None = typer.Option(None, "--data-dir"),
    mode: str = typer.Option("hybrid", "--mode"),
    limit: int = typer.Option(10, "--limit", min=1, max=50),
) -> None:
    """Search the local archive (same engine as MCP)."""
    _boot()
    from slack_cache_mcp.config import load_settings
    from slack_cache_mcp.embeddings.factory import load_embedder
    from slack_cache_mcp.search.service import SearchService
    from slack_cache_mcp.store.sqlite.repo import SqliteRepository

    settings = load_settings(data_dir=data_dir)
    repo = SqliteRepository(settings.archive_db, read_only=True)
    emb = load_embedder(models_dir=settings.models_dir)
    svc = SearchService(repo, embedder=emb)
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


@app.command("export")
def export_cmd(
    out: Path = typer.Option(..., "--out", help="Output directory"),
    fmt: str = typer.Option("slack-export", "--format", help="slack-export|jsonl"),
    data_dir: Path | None = typer.Option(None, "--data-dir"),
    workspace: str | None = typer.Option(None, "--workspace"),
) -> None:
    """Export archive to Slack-export layout or JSONL."""
    _boot()
    from slack_cache_mcp.config import load_settings
    from slack_cache_mcp.export import write_jsonl_export, write_slack_export
    from slack_cache_mcp.store.sqlite.repo import SqliteRepository

    settings = load_settings(data_dir=data_dir)
    repo = SqliteRepository(settings.archive_db, read_only=True)
    if fmt == "slack-export":
        result = write_slack_export(repo, out, workspace=workspace)
    elif fmt == "jsonl":
        result = write_jsonl_export(repo, out, workspace=workspace)
    else:
        typer.echo("Unknown --format (use slack-export|jsonl)", err=True)
        raise typer.Exit(2)
    repo.close()
    typer.echo(json.dumps(result, indent=2, default=str))
    if not result.get("ok"):
        raise typer.Exit(1)


@app.command("mcp")
def mcp_cmd(
    data_dir: Path | None = typer.Option(None, "--data-dir"),
    transport: str = typer.Option("stdio", "--transport"),
) -> None:
    """Run the MCP server (stdio)."""
    _boot()
    if transport != "stdio":
        typer.echo("Only --transport stdio is supported in stage 1", err=True)
        raise typer.Exit(2)
    from slack_cache_mcp.config import load_settings
    from slack_cache_mcp.embeddings.factory import load_embedder
    from slack_cache_mcp.mcp_server.server import run_stdio

    settings = load_settings(data_dir=data_dir)
    emb = load_embedder(models_dir=settings.models_dir)
    run_stdio(settings, embedder=emb)


@app.command("rebuild")
def rebuild_cmd(
    data_dir: Path | None = typer.Option(None, "--data-dir"),
    fts: bool = typer.Option(False, "--fts"),
    embeddings: bool = typer.Option(False, "--embeddings"),
    projection: bool = typer.Option(False, "--projection"),
    all_derived: bool = typer.Option(False, "--all", help="Rebuild docs + embeddings"),
) -> None:
    """Rebuild derived search tables (docs / embeddings)."""
    _boot()
    from slack_cache_mcp.config import load_settings
    from slack_cache_mcp.embeddings.factory import load_embedder
    from slack_cache_mcp.search.docs import SearchDocsBuilder
    from slack_cache_mcp.search.worker import embed_pending
    from slack_cache_mcp.store.sqlite.repo import SqliteRepository

    settings = load_settings(data_dir=data_dir)
    repo = SqliteRepository(settings.archive_db, read_only=False)
    result: dict[str, object] = {"ok": True}
    do_all = all_derived or not (fts or embeddings or projection)
    explicit_hashing = os.environ.get("SLACKCACHE_EMBEDDER", "").lower() == "hashing"
    emb = load_embedder(models_dir=settings.models_dir) if (do_all or embeddings) else None

    def _refuse_demotion(persisted: str, model_id: str) -> None:
        result["ok"] = False
        result["embeddings"] = {
            "ok": False,
            "embedded": 0,
            "error": "refuse_hashing_demotion",
            "persisted_model": persisted,
            "model_id": model_id,
        }
        repo.close()
        typer.echo(json.dumps(result, indent=2, default=str))
        raise typer.Exit(code=1)

    # Projection first so FTS/docs see the repaired current view.
    if do_all or projection:
        rows = repo._conn.execute(  # noqa: SLF001
            "SELECT DISTINCT channel_id, ts FROM message_versions"
        ).fetchall()
        for r in rows:
            repo.reproject_message(r["channel_id"], r["ts"])
        result["projection_rebuilt"] = len(rows)
    if do_all or fts:
        result["fts"] = repo.rebuild_fts()
    if do_all or embeddings:
        assert emb is not None
        # Serialize demotion check + destructive rebuild + embed so a concurrent
        # daemon promotion cannot invalidate the model mid-rebuild.
        started = repo._begin_immediate()  # noqa: SLF001
        try:
            prev = repo._conn.execute(  # noqa: SLF001
                "SELECT value FROM meta WHERE key='embedding_model'"
            ).fetchone()
            if (
                not explicit_hashing
                and prev
                and not str(prev["value"]).startswith("hashing")
                and str(emb.model_id).startswith("hashing")
            ):
                if started:
                    repo._conn.rollback()  # noqa: SLF001
                _refuse_demotion(str(prev["value"]), emb.model_id)
            builder = SearchDocsBuilder(repo)
            dirty_before = repo.peek_dirty_window_channels()
            counts = builder.rebuild_all()
            for ch, gen in dirty_before:
                repo.ack_dirty_window_channel(ch, gen)
            result["search_docs"] = counts
            # Re-check model after rebuild (daemon may have written meta).
            prev2 = repo._conn.execute(  # noqa: SLF001
                "SELECT value FROM meta WHERE key='embedding_model'"
            ).fetchone()
            if (
                not explicit_hashing
                and prev2
                and not str(prev2["value"]).startswith("hashing")
                and str(emb.model_id).startswith("hashing")
            ):
                if started:
                    repo._conn.rollback()  # noqa: SLF001
                _refuse_demotion(str(prev2["value"]), emb.model_id)
            emb_result = embed_pending(
                repo, emb, allow_hashing_demotion=explicit_hashing
            )
            result["embeddings"] = emb_result
            if isinstance(emb_result, dict) and emb_result.get("ok") is False:
                result["ok"] = False
                if started:
                    repo._conn.rollback()  # noqa: SLF001
            elif started:
                repo._conn.commit()  # noqa: SLF001
        except typer.Exit:
            raise
        except Exception:
            if started:
                with contextlib.suppress(Exception):
                    repo._conn.rollback()  # noqa: SLF001
            raise
    elif fts:
        builder = SearchDocsBuilder(repo)
        dirty_before = repo.peek_dirty_window_channels()
        counts = builder.rebuild_all()
        for ch, gen in dirty_before:
            repo.ack_dirty_window_channel(ch, gen)
        result["search_docs"] = counts
    repo.close()
    typer.echo(json.dumps(result, indent=2, default=str))
    if result.get("ok") is False:
        raise typer.Exit(code=1)


models_app = typer.Typer(help="Local embedding model management", no_args_is_help=True)
app.add_typer(models_app, name="models")


@models_app.command("list")
def models_list_cmd(
    data_dir: Path | None = typer.Option(None, "--data-dir"),
) -> None:
    """List local model cache status (no network)."""
    _boot()
    from slack_cache_mcp.config import load_settings
    from slack_cache_mcp.embeddings.factory import load_embedder

    settings = load_settings(data_dir=data_dir)
    emb = load_embedder(models_dir=settings.models_dir)
    typer.echo(
        json.dumps(
            {
                "models_dir": str(settings.models_dir),
                "active_model_id": emb.model_id,
                "dim": emb.dim,
                "models_dir_exists": settings.models_dir.exists(),
            },
            indent=2,
        )
    )


@models_app.command("fetch")
def models_fetch_cmd(
    data_dir: Path | None = typer.Option(None, "--data-dir"),
) -> None:
    """Download embedding model weights from HuggingFace (NOT Slack).

    Intentionally skips netguard so HF download can proceed once. Runtime
    search/daemon still install the guard and set HF_HUB_OFFLINE=1.
    """
    # Do NOT call _boot()/netguard here — PLAN §10.3 / §13.
    from slack_cache_mcp.config import load_settings
    from slack_cache_mcp.embeddings.fastembed_backend import fetch_fastembed_model

    settings = load_settings(data_dir=data_dir)
    try:
        result = fetch_fastembed_model(models_dir=settings.models_dir)
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, indent=2, default=str))


@app.command("eval")
def eval_cmd(
    data_dir: Path | None = typer.Option(None, "--data-dir"),
    private: bool = typer.Option(False, "--private", help="Load private.jsonl metrics only"),
    bench: bool = typer.Option(False, "--bench", help="Run latency bench"),
    bench_n: int = typer.Option(5_000, "--bench-n", help="Synthetic messages for --bench"),
) -> None:
    """Run synthetic search eval (or private local metrics). Offline."""
    _boot()
    import os
    from pathlib import Path as P

    from slack_cache_mcp.config import load_settings
    from slack_cache_mcp.search.eval_harness import bench_hybrid_latency, run_eval
    from slack_cache_mcp.store.sqlite.repo import SqliteRepository

    settings = load_settings(data_dir=data_dir)
    if private:
        from slack_cache_mcp.search.eval_harness import run_private_eval

        path = P(os.environ.get("LOCALAPPDATA", "")) / "slackcache" / "eval" / "private.jsonl"
        # Metrics only — never print query text or expected refs.
        result = run_private_eval(
            settings.archive_db, path, models_dir=settings.models_dir
        )
        typer.echo(json.dumps(result, indent=2, default=str))
        if not result.get("ok"):
            raise typer.Exit(1)
        return

    if bench:
        repo = SqliteRepository(settings.data_dir / "eval-bench.db", read_only=False)
        result = bench_hybrid_latency(repo, n_messages=bench_n)
        repo.close()
        typer.echo(json.dumps(result, indent=2, default=str))
        if not result.get("ok") or not result.get("pass_p95_under_300ms"):
            raise typer.Exit(2)
        return

    repo = SqliteRepository(settings.data_dir / "eval.db", read_only=False)
    result = run_eval(repo)
    repo.close()
    typer.echo(json.dumps(result, indent=2, default=str))
    if not result.get("pass_hybrid_ge_lexical_plus_10pct"):
        raise typer.Exit(2)


@task_app.command("install")
def task_install_cmd(
    data_dir: Path | None = typer.Option(None, "--data-dir"),
) -> None:
    """Register per-user scheduled task (Windows)."""
    _boot()
    from slack_cache_mcp.task_windows import install_task

    result = install_task(data_dir=data_dir)
    typer.echo(json.dumps(result, indent=2, default=str))
    if not result.get("ok"):
        raise typer.Exit(1)


@task_app.command("uninstall")
def task_uninstall_cmd() -> None:
    """Remove the slackcache-daemon scheduled task."""
    _boot()
    from slack_cache_mcp.task_windows import uninstall_task

    result = uninstall_task()
    typer.echo(json.dumps(result, indent=2, default=str))
    if not result.get("ok"):
        raise typer.Exit(1)


@task_app.command("status")
def task_status_cmd() -> None:
    """Show whether the scheduled task is installed."""
    _boot()
    from slack_cache_mcp.task_windows import task_status

    result = task_status()
    typer.echo(json.dumps(result, indent=2, default=str))


def main() -> None:
    # PLAN §10.3 / §13: `models fetch` is the only command allowed network
    # (HuggingFace, never Slack). Skip netguard for that argv only.
    argv = [a.lower() for a in sys.argv[1:] if not a.startswith("-")]
    allow_hf = len(argv) >= 2 and argv[0] == "models" and argv[1] == "fetch"
    if not allow_hf:
        netguard.install()
    app()


if __name__ == "__main__":
    main()
    sys.exit(0)
