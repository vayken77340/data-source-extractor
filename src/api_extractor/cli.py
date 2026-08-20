"""CLI entry point. Phase 1 ships `validate` and `list-sources`."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv

from api_extractor.auth.registry import Authenticator
from api_extractor.config.loader import SOURCES_ROOT, list_sources, load_source, source_path
from api_extractor.config.validate import DEFERRED_CHECKS, Report, validate_source
from api_extractor.http.client import Client
from api_extractor.logs import configure_logging
from api_extractor.persist import manifest
from api_extractor.plan.binding import Plan, build_plan
from api_extractor import providers
from api_extractor.providers import registry
from api_extractor.runner import context_for, execute

app = typer.Typer(add_completion=False, help="Config-driven API sample extractor.")


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    load_dotenv()
    configure_logging(logging.DEBUG if verbose else logging.INFO)
    providers.load_from()


@app.command("run")
def run_command(
    source: Annotated[str, typer.Argument(help="Source name, e.g. thingsboard.")],
    endpoints: Annotated[
        list[str] | None,
        typer.Option("--endpoint", help="Run a subset. Repeatable."),
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Cap values fanned out per endpoint.")
    ] = None,
    no_limit: Annotated[bool, typer.Option("--no-limit", help="Remove every limit.")] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the resolved plan and issue nothing.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Rewrite outputs that already exist.")
    ] = False,
    run_id: Annotated[
        str | None, typer.Option("--run-id", help="Reuse a run id instead of minting one.")
    ] = None,
) -> None:
    """Issue the requests a source defines. Validation runs first, always."""
    path = source_path(source)
    report = validate_source(path)
    if not report.ok:
        _print_report(report, show_checks=False)
        raise typer.Exit(1)

    config = load_source(path)
    unknown = sorted(set(endpoints or ()) - set(config.endpoints))
    if unknown:
        typer.echo(f"no such endpoint(s): {', '.join(unknown)}")
        raise typer.Exit(1)

    output_root = Path("output")
    run = run_id or manifest.new_run_id()
    only = tuple(endpoints or ())

    if dry_run:
        context = context_for(config, run, output_root)
        _print_plan(build_plan(config, context, only=only, limit=limit, no_limit=no_limit))
        return

    with Client(retries=config.defaults.retries, rate_limit=config.defaults.rate_limit) as client:
        authenticator = Authenticator(config.auth, client, config.base_url)
        result = execute(
            config,
            client,
            authenticator,
            output_root=output_root,
            run_id=run,
            force=force,
            only=only,
            limit=limit,
            no_limit=no_limit,
        )

    typer.echo(
        f"{result.run_id}: {result.written} written, {result.skipped} skipped, "
        f"{result.failed} failed"
    )
    typer.echo(f"manifest: {result.manifest_path}")
    if result.failed:
        raise typer.Exit(1)


@app.command("validate")
def validate_command(
    source: Annotated[str, typer.Argument(help="Source name, e.g. thingsboard.")],
    show_checks: Annotated[
        bool, typer.Option("--show-checks", help="List the checks that ran.")
    ] = False,
) -> None:
    """Check a source definition, reporting every problem in one pass."""
    path = source_path(source)
    report = validate_source(path)
    _print_report(report, show_checks=show_checks)
    raise typer.Exit(0 if report.ok else 1)


@app.command("list-sources")
def list_sources_command() -> None:
    """List the source definitions in config/sources."""
    names = list_sources()
    if not names:
        typer.echo(f"no source definitions in {SOURCES_ROOT}")
        return
    for name in names:
        typer.echo(name)


@app.command("list-providers")
def list_providers_command() -> None:
    """List the registered providers and the args each takes."""
    for entry in registry.registered():
        chains = "  (chains off an endpoint)" if entry.depends_on is not None else ""
        typer.echo(f"{entry.name}({', '.join(entry.arg_names())}){chains}")


def _print_plan(plan: Plan) -> None:
    typer.echo(f"{plan.source} — dag order: {', '.join(plan.order) or 'nothing selected'}")

    if plan.provider_rows:
        typer.echo("")
        typer.echo("providers")
        width = max(len(name) for name in plan.provider_rows)
        for name, rows in plan.provider_rows.items():
            typer.echo(f"  {name:<{width}}  {rows} row(s)")

    if plan.endpoints:
        typer.echo("")
        typer.echo("endpoints")
        width = max(len(item.endpoint) for item in plan.endpoints)
        for item in plan.endpoints:
            detail = item.error if item.error else f"{len(item.requests)} request(s)"
            marker = "!" if item.error else " "
            typer.echo(f"{marker} {item.endpoint:<{width}}  {detail}")

    typer.echo("")
    typer.echo(f"total {plan.request_count} request(s)")
    if plan.failures:
        typer.echo(f"{len(plan.failures)} endpoint(s) could not be planned")


def _print_report(report: Report, *, show_checks: bool) -> None:
    if show_checks or not report.ok:
        typer.echo(f"checks run ({len(report.checks_run)}): {', '.join(report.checks_run)}")
        for check_id in report.checks_deferred:
            typer.echo(f"  deferred: {check_id} — {DEFERRED_CHECKS[check_id]}")
    if report.ok:
        typer.echo(f"{report.path}: ok")
        return
    typer.echo("")
    typer.echo(f"{report.path}: {len(report.issues)} problem(s)")
    for issue in report.issues:
        typer.echo(f"  {issue}")


if __name__ == "__main__":  # pragma: no cover
    app()
