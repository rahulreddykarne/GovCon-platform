"""Typer CLI. Every admin action in this phase is exposed here."""

from __future__ import annotations

import os

import typer
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import select

from govcon.config import ConfigError, Settings, get_settings
from govcon.db import check_connectivity, make_engine, session_scope
from govcon.logging import configure_logging, redact
from govcon.models import User
from govcon.paths import repo_root
from govcon.seed import demo_watchlist_count, seed_demo_watchlist

app = typer.Typer(help="GovCon opportunity and bid management platform.", no_args_is_help=True)
db_app = typer.Typer(help="Database administration.")
users_app = typer.Typer(help="Invite-only user administration.")
ingest_app = typer.Typer(help="Source ingestion.")
app.add_typer(db_app, name="db")
app.add_typer(users_app, name="users")
app.add_typer(ingest_app, name="ingest")


def main() -> None:
    app()


def _settings() -> Settings:
    try:
        return get_settings()
    except ValidationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc


def _fail_config(exc: ConfigError) -> None:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=2)


def alembic_config() -> Config:
    root = repo_root()
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    return config


@app.callback()
def _bootstrap() -> None:
    try:
        configure_logging(_settings())
    except ValidationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc


@app.command("status")
def status() -> None:
    """Print database connectivity and the applied schema revision."""
    try:
        settings = _settings()
        engine = make_engine(settings)
    except ConfigError as exc:
        _fail_config(exc)
        return
    ok, revision = check_connectivity(engine)
    if ok:
        typer.echo("database_connectivity: ok")
        typer.echo(f"schema_revision: {revision}")
        return
    typer.echo("database_connectivity: failed")
    typer.echo("schema_revision: unavailable")
    raise typer.Exit(code=1)


@db_app.command("upgrade")
def db_upgrade(revision: str = typer.Argument("head")) -> None:
    """Apply Alembic migrations up to the given revision."""
    try:
        _settings().require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    os.environ.pop("GOVCON_ALEMBIC_URL", None)
    command.upgrade(alembic_config(), revision)


@db_app.command("seed-demo-watchlist")
def db_seed_demo_watchlist() -> None:
    """Insert the empty demo watchlist. Safe to run more than once."""
    try:
        _settings().require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    with session_scope() as session:
        _row, created = seed_demo_watchlist(session)
        count = demo_watchlist_count(session)
    typer.echo(f"demo_watchlist_created: {'yes' if created else 'no'}")
    typer.echo(f"demo_watchlist_count: {count}")


@users_app.command("invite")
def users_invite(
    email: str = typer.Option(..., help="Login email."),
    display_name: str = typer.Option(..., help="Name shown on reviews and audit events."),
    role: str = typer.Option("reviewer", help="owner, approver, reviewer, or read_only."),
    password: str = typer.Option(
        ...,
        prompt=True,
        hide_input=True,
        help="Password. Prefer the prompt so the shell history does not store it.",
    ),
) -> None:
    """Create one user. There is no self-registration command."""
    from govcon.collaboration.users import invite_user

    try:
        _settings().require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    try:
        with session_scope() as session:
            user = invite_user(
                session,
                email=email,
                display_name=display_name,
                password=password,
                role=role,
            )
            created_email = user.email
            created_role = user.role
    except ValueError as exc:
        typer.echo(redact(str(exc)), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"user_invited: {created_email}")
    typer.echo(f"role: {created_role}")


@users_app.command("deactivate")
def users_deactivate(email: str = typer.Option(..., help="Email of the user to disable.")) -> None:
    """Disable a user and revoke that user's open sessions."""
    from govcon.collaboration.users import deactivate_user

    try:
        _settings().require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    normalized = email.strip().lower()
    with session_scope() as session:
        user = session.scalar(select(User).where(User.email == normalized))
        if user is None:
            typer.echo("user not found", err=True)
            raise typer.Exit(code=2)
        deactivate_user(session, user)
    typer.echo(f"user_deactivated: {normalized}")


@users_app.command("list")
def users_list() -> None:
    """List users. Password hashes are never printed."""
    try:
        _settings().require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    with session_scope() as session:
        users = session.scalars(select(User).order_by(User.email)).all()
        rows = [(user.email, user.display_name, user.role, user.is_active) for user in users]
    for email, display_name, role, is_active in rows:
        state = "active" if is_active else "inactive"
        typer.echo(f"{email}\t{display_name}\t{role}\t{state}")


def _echo_ingest(run_id: int, status: str, stats) -> None:
    typer.echo(f"fetched: {stats.fetched}")
    typer.echo(f"inserted: {stats.inserted}")
    typer.echo(f"updated: {stats.updated}")
    typer.echo(f"unchanged: {stats.unchanged}")
    typer.echo(f"errors: {len(stats.errors)}")
    typer.echo(f"run_id: {run_id}")
    typer.echo(f"status: {status}")


def _posted_window_from_options(posted_from: str | None, posted_to: str | None, *, default_recent: bool):
    from govcon.ingest.sam_opportunities import default_posted_window, parse_user_date

    if posted_from is None and posted_to is None:
        if not default_recent:
            typer.echo("posted-from and posted-to are required", err=True)
            raise typer.Exit(code=2)
        return default_posted_window()
    if posted_from is None or posted_to is None:
        typer.echo("posted-from and posted-to must be provided together", err=True)
        raise typer.Exit(code=2)
    try:
        return parse_user_date(posted_from), parse_user_date(posted_to)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc


def _run_status(stats) -> str:
    if stats.errors:
        return "completed_with_errors"
    return "succeeded"


@ingest_app.command("sam")
def ingest_sam(
    posted_from: str | None = typer.Option(
        None,
        help="Posted-from date (MM/dd/yyyy or YYYY-MM-DD). Defaults to the last 3 UTC days.",
    ),
    posted_to: str | None = typer.Option(
        None,
        help="Posted-to date (MM/dd/yyyy or YYYY-MM-DD). Defaults to today UTC.",
    ),
    limit: int = typer.Option(1000, min=1, max=1000, help="Records per page. SAM.gov maximum is 1000."),
) -> None:
    """Ingest SAM.gov opportunities. The default window is the last 3 days."""
    from govcon.ingest.runs import IngestStats, finish_run, start_run
    from govcon.ingest.sam_opportunities import SamApiError, assert_search_window, pull_sam_opportunities
    from govcon.logging import redact

    window_from, window_to = _posted_window_from_options(posted_from, posted_to, default_recent=True)
    try:
        assert_search_window(window_from, window_to)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    try:
        settings = _settings()
        settings.require_database_url()
        api_key = settings.require_sam_api_key()
    except ConfigError as exc:
        _fail_config(exc)
        return
    status = "succeeded"
    stats = IngestStats()
    with session_scope(settings) as session:
        run = start_run(session, "sam_opportunities")
        try:
            stats = pull_sam_opportunities(
                session,
                api_key=api_key,
                posted_from=window_from,
                posted_to=window_to,
                limit=limit,
                settings=settings,
            )
        except (SamApiError, ValueError) as exc:
            stats = IngestStats(errors=[redact(str(exc))])
            status = "failed"
        status = status if status == "failed" else _run_status(stats)
        finish_run(run, stats, status=status)
        run_id = run.id
    _echo_ingest(run_id, status, stats)
    if status != "succeeded":
        raise typer.Exit(code=1)


@ingest_app.command("sam-backfill")
def ingest_sam_backfill(
    posted_from: str = typer.Option(..., help="Posted-from date (MM/dd/yyyy or YYYY-MM-DD)."),
    posted_to: str = typer.Option(..., help="Posted-to date (MM/dd/yyyy or YYYY-MM-DD)."),
    limit: int = typer.Option(1000, min=1, max=1000, help="Records per page. SAM.gov maximum is 1000."),
) -> None:
    """Backfill SAM.gov opportunities in posted-date windows of at most one year."""
    from govcon.ingest.runs import IngestStats, finish_run, start_run
    from govcon.ingest.sam_opportunities import (
        SamApiError,
        backfill_sam_opportunities,
        iter_backfill_windows,
        parse_user_date,
    )
    from govcon.logging import redact

    try:
        window_from = parse_user_date(posted_from)
        window_to = parse_user_date(posted_to)
        iter_backfill_windows(window_from, window_to)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    try:
        settings = _settings()
        settings.require_database_url()
        api_key = settings.require_sam_api_key()
    except ConfigError as exc:
        _fail_config(exc)
        return
    totals = IngestStats()
    run_id = 0
    status = "succeeded"
    with session_scope(settings) as session:
        run = start_run(session, "sam_backfill")
        try:
            totals = backfill_sam_opportunities(
                session,
                api_key=api_key,
                posted_from=window_from,
                posted_to=window_to,
                limit=limit,
                settings=settings,
            )
        except (SamApiError, ValueError) as exc:
            totals.errors.append(redact(str(exc)))
            finish_run(run, totals, status="failed")
            run_id = run.id
            status = "failed"
        else:
            status = _run_status(totals)
            finish_run(run, totals, status=status)
            run_id = run.id
    _echo_ingest(run_id, status, totals)
    if status != "succeeded":
        raise typer.Exit(code=1)


@ingest_app.command("sam-archive-sweep")
def ingest_sam_archive_sweep() -> None:
    """Archive SAM rows whose archive date has passed. Does not call the network."""
    from govcon.ingest.runs import finish_run, start_run
    from govcon.ingest.sam_opportunities import archive_expired_sam_opportunities

    try:
        settings = _settings()
        settings.require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    with session_scope(settings) as session:
        run = start_run(session, "sam_archive_sweep")
        stats = archive_expired_sam_opportunities(session)
        status = _run_status(stats)
        finish_run(run, stats, status=status)
        run_id = run.id
    _echo_ingest(run_id, status, stats)
