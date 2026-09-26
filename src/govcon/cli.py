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
app.add_typer(db_app, name="db")
app.add_typer(users_app, name="users")


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
