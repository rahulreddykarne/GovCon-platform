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
match_app = typer.Typer(help="Watchlist matching.")
watchlist_app = typer.Typer(help="Watchlist administration.")
alerts_app = typer.Typer(help="Alert digests.")
awards_app = typer.Typer(help="Award history and pricing.")
vendors_app = typer.Typer(help="Vendor profiles and competitor intelligence.")
contacts_app = typer.Typer(help="Buyer contact search.")
app.add_typer(db_app, name="db")
app.add_typer(users_app, name="users")
app.add_typer(ingest_app, name="ingest")
app.add_typer(match_app, name="match")
app.add_typer(watchlist_app, name="watchlist")
app.add_typer(alerts_app, name="alerts")
app.add_typer(awards_app, name="awards")
app.add_typer(vendors_app, name="vendors")
app.add_typer(contacts_app, name="contacts")


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


def _echo_dibbs(run_id: int, status: str, result) -> None:
    coverage = result.coverage
    typer.echo(f"index_file: {result.index_name}")
    if result.retained_path:
        typer.echo(f"retained: {result.retained_path}")
    typer.echo(f"records: {coverage.records}")
    typer.echo(f"nsn_coverage: {coverage.nsn}/{coverage.records}")
    typer.echo(f"quantity_coverage: {coverage.quantity}/{coverage.records}")
    _echo_ingest(run_id, status, result.stats)


@ingest_app.command("dibbs")
def ingest_dibbs(
    file: str | None = typer.Option(
        None,
        "--file",
        help="Local inYYMMDD.txt index to ingest. Does not call the network.",
    ),
    posted_date: str | None = typer.Option(
        None,
        "--date",
        help="Index post date (YYYY-MM-DD or MM/dd/yyyy). Default is the newest file on the recent RFQ page.",
    ),
) -> None:
    """Ingest one DIBBS daily index file. Re-running an unchanged file does not add snapshots."""
    from pathlib import Path

    from govcon.ingest.dibbs import DibbsError, ingest_index_file, parse_user_date, pull_dibbs_index
    from govcon.ingest.runs import IngestStats, finish_run, start_run
    from govcon.logging import redact

    if file and posted_date:
        typer.echo("pass either --file or --date, not both", err=True)
        raise typer.Exit(code=2)
    day = None
    if posted_date:
        try:
            day = parse_user_date(posted_date)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
    local_path: Path | None = None
    if file:
        local_path = Path(file)
        if not local_path.is_file():
            typer.echo(f"index file not found: {file}", err=True)
            raise typer.Exit(code=2)
    try:
        settings = _settings()
        settings.require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    status = "succeeded"
    stats = IngestStats()
    result = None
    with session_scope(settings) as session:
        run = start_run(session, "dibbs_index")
        try:
            if local_path is not None:
                result = ingest_index_file(session, local_path, data_dir=settings.data_dir)
            else:
                result = pull_dibbs_index(session, settings=settings, posted_date=day)
            stats = result.stats
        except (DibbsError, OSError, ValueError) as exc:
            stats = IngestStats(errors=[redact(str(exc))])
            status = "failed"
        status = status if status == "failed" else _run_status(stats)
        finish_run(run, stats, status=status)
        run_id = run.id
    if result is None:
        _echo_ingest(run_id, status, stats)
    else:
        _echo_dibbs(run_id, status, result)
    if status != "succeeded":
        raise typer.Exit(code=1)


@ingest_app.command("usaspending")
def ingest_usaspending(
    backfill: bool = typer.Option(False, "--backfill", help="Force the 3-year action-date backfill."),
    window_from: str | None = typer.Option(None, "--from", help="Window start (YYYY-MM-DD or MM/dd/yyyy)."),
    window_to: str | None = typer.Option(None, "--to", help="Window end (YYYY-MM-DD or MM/dd/yyyy)."),
    modified: bool = typer.Option(
        False,
        "--modified",
        help="For an explicit window, filter on last_modified_date instead of action_date.",
    ),
    file: str | None = typer.Option(
        None,
        "--file",
        help="Ingest a local spending_by_award JSON file. Does not call the network.",
    ),
) -> None:
    """Pull USAspending contract awards for enabled watchlist PSC and NAICS codes.

    The first successful run uses a 3-year action-date lookback. Later runs
    pull last-modified awards since that window, with one day of overlap.
    """
    from pathlib import Path

    from govcon.ingest.runs import IngestStats, finish_run, start_run
    from govcon.ingest.usaspending import (
        JOB_NAME,
        UsaSpendingError,
        ingest_award_records,
        load_search_document,
        parse_user_date,
        plan_pull,
        pull_usaspending,
    )
    from govcon.logging import redact

    if file and (backfill or window_from or window_to or modified):
        typer.echo("pass either --file or a date window, not both", err=True)
        raise typer.Exit(code=2)
    if modified and not (window_from and window_to):
        typer.echo("--modified applies to an explicit --from/--to window", err=True)
        raise typer.Exit(code=2)
    local_path: Path | None = None
    if file:
        local_path = Path(file)
        if not local_path.is_file():
            typer.echo(f"award file not found: {file}", err=True)
            raise typer.Exit(code=2)
    start = end = None
    if window_from or window_to:
        if not window_from or not window_to:
            typer.echo("--from and --to must be provided together", err=True)
            raise typer.Exit(code=2)
        try:
            start = parse_user_date(window_from)
            end = parse_user_date(window_to)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
    try:
        settings = _settings()
        settings.require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    status = "succeeded"
    stats = IngestStats()
    details: dict = {"mode": "file"} if local_path is not None else {}
    with session_scope(settings) as session:
        run = start_run(session, JOB_NAME)
        try:
            if local_path is not None:
                stats = ingest_award_records(session, load_search_document(local_path))
                details = {"mode": "file"}
            else:
                plan = plan_pull(
                    session,
                    force_backfill=backfill,
                    start=start,
                    end=end,
                    date_type="last_modified_date" if modified else None,
                )
                details = {
                    "mode": plan.mode,
                    "date_type": plan.date_type,
                    "window_start": plan.start.isoformat(),
                    "window_end": plan.end.isoformat(),
                }
                typer.echo(f"mode: {plan.mode}")
                typer.echo(f"date_type: {plan.date_type}")
                typer.echo(f"window: {plan.start.isoformat()}..{plan.end.isoformat()}")
                stats = pull_usaspending(session, plan, settings=settings)
        except (UsaSpendingError, OSError, ValueError) as exc:
            attached = getattr(exc, "stats", None)
            stats = attached if isinstance(attached, IngestStats) else stats
            message = redact(str(exc))
            if message not in stats.errors:
                stats.errors.append(message)
            status = "failed"
        status = status if status == "failed" else _run_status(stats)
        finish_run(run, stats, status=status, details=details or None)
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


def _parse_decimal(value: str | None):
    from decimal import Decimal

    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return Decimal(cleaned)


def _parse_csv_option(value: str | None) -> list[str] | None:
    if value is None:
        return None
    items = [item.strip() for item in value.split(",") if item.strip()]
    return items


def _echo_match_stats(stats) -> None:
    typer.echo(f"evaluated: {stats.evaluated}")
    typer.echo(f"matched: {stats.matched}")
    typer.echo(f"inserted: {stats.inserted}")
    typer.echo(f"updated: {stats.updated}")
    typer.echo(f"unchanged: {stats.unchanged}")
    typer.echo(f"removed: {stats.removed}")


@match_app.command("run")
def match_run() -> None:
    """Evaluate all enabled watchlists and upsert matches."""
    from govcon.matching.engine import run_matching

    try:
        _settings().require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    with session_scope() as session:
        stats = run_matching(session)
    _echo_match_stats(stats)


@match_app.command("rebuild")
def match_rebuild(
    watchlist: int = typer.Option(..., "--watchlist", help="Watchlist id to rebuild."),
) -> None:
    """Re-evaluate one watchlist and remove stale matches."""
    from govcon.matching.engine import run_matching

    try:
        _settings().require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    with session_scope() as session:
        try:
            stats = run_matching(session, watchlist_id=watchlist, rebuild=True)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
    _echo_match_stats(stats)


@watchlist_app.command("add")
def watchlist_add(
    name: str = typer.Option(..., help="Watchlist name."),
    psc_codes: str | None = typer.Option(None, help="Comma-separated PSC prefixes."),
    naics_codes: str | None = typer.Option(None, help="Comma-separated NAICS prefixes."),
    keywords: str | None = typer.Option(None, help="Comma-separated keywords."),
    exclude_keywords: str | None = typer.Option(None, help="Comma-separated exclude keywords."),
    nsn_list: str | None = typer.Option(None, help="Comma-separated NSNs."),
    set_asides: str | None = typer.Option(None, help="Comma-separated set-aside codes."),
    sources: str | None = typer.Option("sam,dibbs,usaspending", help="Comma-separated sources."),
    min_value: str | None = typer.Option(None, help="Minimum estimated value."),
    max_value: str | None = typer.Option(None, help="Maximum estimated value."),
    min_deadline_days: int | None = typer.Option(None, help="Minimum days until deadline."),
    notes: str | None = typer.Option(None, help="Operator notes."),
) -> None:
    """Create a watchlist."""
    from govcon.matching.watchlists import create_watchlist

    try:
        _settings().require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    with session_scope() as session:
        row = create_watchlist(
            session,
            name=name,
            psc_codes=_parse_csv_option(psc_codes),
            naics_codes=_parse_csv_option(naics_codes),
            keywords=_parse_csv_option(keywords),
            exclude_keywords=_parse_csv_option(exclude_keywords),
            nsn_list=_parse_csv_option(nsn_list),
            set_asides=_parse_csv_option(set_asides),
            sources=_parse_csv_option(sources),
            min_value=_parse_decimal(min_value),
            max_value=_parse_decimal(max_value),
            min_deadline_days=min_deadline_days,
            notes=notes,
        )
        watchlist_id = row.id
    typer.echo(f"watchlist_id: {watchlist_id}")
    typer.echo(f"name: {name}")


@watchlist_app.command("list")
def watchlist_list(
    include_disabled: bool = typer.Option(True, help="Include disabled watchlists."),
) -> None:
    """List watchlists."""
    from govcon.matching.watchlists import list_watchlists

    try:
        _settings().require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    with session_scope() as session:
        rows = list_watchlists(session, include_disabled=include_disabled)
    for row in rows:
        state = "enabled" if row.enabled else "disabled"
        typer.echo(
            f"{row.id}\t{row.name}\t{state}\tpsc={row.psc_codes or []}\t"
            f"naics={row.naics_codes or []}\tkeywords={row.keywords or []}"
        )


@watchlist_app.command("edit")
def watchlist_edit(
    watchlist_id: int = typer.Option(..., help="Watchlist id."),
    name: str | None = typer.Option(None, help="New name."),
    psc_codes: str | None = typer.Option(None, help="Comma-separated PSC prefixes."),
    naics_codes: str | None = typer.Option(None, help="Comma-separated NAICS prefixes."),
    keywords: str | None = typer.Option(None, help="Comma-separated keywords."),
    exclude_keywords: str | None = typer.Option(None, help="Comma-separated exclude keywords."),
    nsn_list: str | None = typer.Option(None, help="Comma-separated NSNs."),
    set_asides: str | None = typer.Option(None, help="Comma-separated set-aside codes."),
    sources: str | None = typer.Option(None, help="Comma-separated sources."),
    min_value: str | None = typer.Option(None, help="Minimum estimated value."),
    max_value: str | None = typer.Option(None, help="Maximum estimated value."),
    min_deadline_days: int | None = typer.Option(None, help="Minimum days until deadline."),
    notes: str | None = typer.Option(None, help="Operator notes."),
    enabled: bool | None = typer.Option(None, help="Enable or disable the watchlist."),
) -> None:
    """Update one watchlist field set."""
    from govcon.matching.watchlists import get_watchlist, update_watchlist

    try:
        _settings().require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    updates: dict[str, object] = {}
    if name is not None:
        updates["name"] = name
    if psc_codes is not None:
        updates["psc_codes"] = _parse_csv_option(psc_codes)
    if naics_codes is not None:
        updates["naics_codes"] = _parse_csv_option(naics_codes)
    if keywords is not None:
        updates["keywords"] = _parse_csv_option(keywords)
    if exclude_keywords is not None:
        updates["exclude_keywords"] = _parse_csv_option(exclude_keywords)
    if nsn_list is not None:
        updates["nsn_list"] = _parse_csv_option(nsn_list)
    if set_asides is not None:
        updates["set_asides"] = _parse_csv_option(set_asides)
    if sources is not None:
        updates["sources"] = _parse_csv_option(sources)
    if min_value is not None:
        updates["min_value"] = _parse_decimal(min_value)
    if max_value is not None:
        updates["max_value"] = _parse_decimal(max_value)
    if min_deadline_days is not None:
        updates["min_deadline_days"] = min_deadline_days
    if notes is not None:
        updates["notes"] = notes
    if enabled is not None:
        updates["enabled"] = enabled
    if not updates:
        typer.echo("no fields to update", err=True)
        raise typer.Exit(code=2)
    with session_scope() as session:
        row = get_watchlist(session, watchlist_id)
        if row is None:
            typer.echo("watchlist not found", err=True)
            raise typer.Exit(code=2)
        update_watchlist(session, row, **updates)
    typer.echo(f"watchlist_updated: {watchlist_id}")


def _echo_digest(result) -> None:
    typer.echo(f"message: {'yes' if result.sent else 'no'}")
    typer.echo(f"channel: {result.channel}")
    if result.path:
        typer.echo(f"path: {result.path}")
    typer.echo(f"new_matches: {result.new_count}")
    typer.echo(f"amendment_alerts: {result.amendment_count}")


@alerts_app.command("digest")
def alerts_digest() -> None:
    """Send unalerted new matches, grouped by watchlist, and optional deadline re-alerts."""
    from govcon.alerts.digest import DigestDeliveryError, run_digest

    try:
        _settings().require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    try:
        with session_scope() as session:
            result = run_digest(session, settings=_settings())
    except DigestDeliveryError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    _echo_digest(result)


def _echo_award_count(count: int) -> None:
    typer.echo(f"count: {count}")


def _echo_price_points(rows) -> None:
    _echo_award_count(len(rows))
    for row in rows:
        typer.echo(f"award_id: {row.award_id}")
        typer.echo(f"vendor: {row.vendor_name or ''}")
        typer.echo(f"date: {row.action_date.isoformat() if row.action_date else ''}")
        typer.echo(f"amount: {_decimal_text(row.amount)}")
        if row.unit_price is not None:
            typer.echo(f"unit_price: {_decimal_text(row.unit_price)}")
        typer.echo("---")


def _decimal_text(value) -> str:
    if value is None:
        return ""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


@awards_app.command("price-history")
def awards_price_history(
    nsn: str = typer.Option(..., "--nsn", help="NSN, with or without dashes."),
) -> None:
    """Show stored award history for one NSN."""
    from govcon.matching.pricing import price_history

    try:
        _settings().require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    with session_scope() as session:
        rows = price_history(session, nsn)
    _echo_price_points(rows)


@awards_app.command("price-history-psc")
def awards_price_history_psc(
    psc: str = typer.Option(..., "--psc", help="PSC code or prefix."),
    keywords: str | None = typer.Option(None, "--keywords", help="Comma-separated whole-word keywords."),
) -> None:
    """Show stored awards for a PSC prefix, optionally filtered by keywords."""
    from govcon.matching.pricing import price_history_psc

    try:
        _settings().require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    with session_scope() as session:
        rows = price_history_psc(session, psc, _parse_csv_option(keywords))
    _echo_price_points(rows)


@awards_app.command("history")
def awards_history(
    agency: str = typer.Option(..., "--agency", help="Awarding agency text to match."),
    psc: str | None = typer.Option(None, "--psc", help="Optional PSC code or prefix."),
    naics: str | None = typer.Option(None, "--naics", help="Optional NAICS code or prefix."),
    nsn: str | None = typer.Option(None, "--nsn", help="Optional NSN."),
) -> None:
    """Show stored awards for an awarding agency."""
    from govcon.intelligence.awards import award_history_for_agency

    try:
        _settings().require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    with session_scope() as session:
        rows = award_history_for_agency(session, agency, psc=psc, naics=naics, nsn=nsn)
    _echo_price_points(rows)


@awards_app.command("top")
def awards_top(
    psc: str | None = typer.Option(None, "--psc", help="Optional PSC code or prefix."),
    naics: str | None = typer.Option(None, "--naics", help="Optional NAICS code or prefix."),
    nsn: str | None = typer.Option(None, "--nsn", help="Optional NSN."),
    limit: int = typer.Option(10, min=1, help="Maximum recipients to print."),
) -> None:
    """Show recipients with the most stored awards. Does not print a unit price."""
    from govcon.intelligence.awards import top_awardees

    try:
        _settings().require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    with session_scope() as session:
        rows = top_awardees(session, psc=psc, naics=naics, nsn=nsn, limit=limit)
    _echo_award_count(len(rows))
    for row in rows:
        typer.echo(f"recipient_uei: {row.recipient_uei or ''}")
        typer.echo(f"recipient_name: {row.recipient_name or ''}")
        typer.echo(f"award_count: {row.award_count}")
        typer.echo(f"total_obligation: {_decimal_text(row.total_obligation)}")
        typer.echo("---")


@awards_app.command("recompete")
def awards_recompete(
    psc: str | None = typer.Option(None, "--psc", help="Optional PSC code or prefix."),
    naics: str | None = typer.Option(None, "--naics", help="Optional NAICS code or prefix."),
    nsn: str | None = typer.Option(None, "--nsn", help="Optional NSN."),
) -> None:
    """List older awards that may be recompeted."""
    from govcon.intelligence.awards import recompete_candidates

    try:
        _settings().require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    with session_scope() as session:
        rows = recompete_candidates(session, psc=psc, naics=naics, nsn=nsn)
    _echo_award_count(len(rows))
    for row in rows:
        typer.echo(f"award_id: {row.award_id}")
        typer.echo(f"heuristic: {row.heuristic}")
        typer.echo(f"action_date: {row.action_date.isoformat() if row.action_date else ''}")
        typer.echo(f"period_end: {row.period_end.isoformat() if row.period_end else ''}")
        typer.echo(f"vendor: {row.recipient_name or ''}")
        typer.echo("---")


def _echo_vendor_profile(profile) -> None:
    typer.echo(f"uei: {profile.uei}")
    typer.echo(f"cage: {profile.cage_code or ''}")
    typer.echo(f"legal_name: {profile.legal_name or ''}")
    typer.echo(f"dba_name: {profile.dba_name or ''}")
    typer.echo(f"registration_status: {profile.registration_status or ''}")
    typer.echo(f"fetched_at: {profile.fetched_at.isoformat() if profile.fetched_at else ''}")
    typer.echo(f"from_cache: {'yes' if profile.from_cache else 'no'}")
    if profile.business_types:
        typer.echo(f"business_types: {profile.business_types}")
    if profile.naics_codes:
        typer.echo(f"naics_codes: {profile.naics_codes}")
    if profile.psc_codes:
        typer.echo(f"psc_codes: {profile.psc_codes}")
    stats = profile.award_stats
    typer.echo(f"award_count: {stats.award_count}")
    typer.echo(f"total_obligation: {_decimal_text(stats.total_obligation)}")
    for agency in stats.top_agencies:
        typer.echo(
            f"top_agency: {agency.label}\tcount={agency.count}\t"
            f"obligation={_decimal_text(agency.total_obligation)}"
        )
    for psc in stats.top_pscs:
        typer.echo(
            f"top_psc: {psc.label}\tcount={psc.count}\t"
            f"obligation={_decimal_text(psc.total_obligation)}"
        )


@vendors_app.command("show")
def vendors_show(
    uei: str = typer.Option(..., "--uei", help="12-character Unique Entity Identifier."),
    refresh: bool = typer.Option(False, help="Ignore the cache and call SAM.gov."),
) -> None:
    """Show a vendor profile with computed historical award statistics."""
    from govcon.intelligence.vendors import SamEntityError, vendor_profile

    try:
        settings = _settings()
        settings.require_database_url()
    except (ConfigError, ValidationError) as exc:
        if isinstance(exc, ConfigError):
            _fail_config(exc)
        else:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        return
    try:
        with session_scope() as session:
            profile = vendor_profile(session, uei, refresh=refresh, settings=settings)
    except SamEntityError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    _echo_vendor_profile(profile)


def _echo_competitor_summary(summary) -> None:
    typer.echo(f"opportunity_id: {summary.opportunity_id}")
    typer.echo(f"bucket_count: {len(summary.buckets)}")
    for bucket in summary.buckets:
        typer.echo(f"dimension: {bucket.dimension}")
        typer.echo(f"label: {bucket.label}")
        for winner in bucket.winners:
            typer.echo(f"recipient_uei: {winner.recipient_uei or ''}")
            typer.echo(f"recipient_name: {winner.recipient_name or ''}")
            typer.echo(f"award_count: {winner.award_count}")
            typer.echo(f"total_obligation: {_decimal_text(winner.total_obligation)}")
        typer.echo("---")


@vendors_app.command("competitors")
def vendors_competitors(
    opportunity_id: int = typer.Option(..., help="Stored opportunity id."),
    limit: int = typer.Option(5, min=1, help="Maximum winners per dimension."),
) -> None:
    """Show likely historical competitors for one opportunity."""
    from govcon.intelligence.competitors import competitor_summary

    try:
        _settings().require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    with session_scope() as session:
        summary = competitor_summary(session, opportunity_id, limit=limit)
    if summary is None:
        typer.echo("opportunity not found", err=True)
        raise typer.Exit(code=2)
    _echo_competitor_summary(summary)


@contacts_app.command("search")
def contacts_search(
    name: str | None = typer.Option(None, help="Contact name substring."),
    agency: str | None = typer.Option(None, help="Agency path substring."),
    email: str | None = typer.Option(None, help="Email substring."),
    limit: int = typer.Option(50, min=1, help="Maximum rows to print."),
) -> None:
    """Search buyer contacts harvested from opportunities."""
    from govcon.intelligence.contacts import search_contacts

    try:
        _settings().require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    with session_scope() as session:
        rows = search_contacts(session, name=name, agency=agency, email=email, limit=limit)
    typer.echo(f"count: {len(rows)}")
    for row in rows:
        typer.echo(f"id: {row.id}")
        typer.echo(f"name: {row.name or ''}")
        typer.echo(f"email: {row.email or ''}")
        typer.echo(f"phone: {row.phone or ''}")
        typer.echo(f"title: {row.title or ''}")
        typer.echo(f"agency_path: {row.agency_path or ''}")
        typer.echo(f"contact_type: {row.contact_type or ''}")
        typer.echo("---")


@watchlist_app.command("disable")
def watchlist_disable(
    watchlist_id: int = typer.Option(..., help="Watchlist id."),
) -> None:
    """Disable a watchlist."""
    from govcon.matching.watchlists import disable_watchlist, get_watchlist

    try:
        _settings().require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    with session_scope() as session:
        row = get_watchlist(session, watchlist_id)
        if row is None:
            typer.echo("watchlist not found", err=True)
            raise typer.Exit(code=2)
        disable_watchlist(session, row)
    typer.echo(f"watchlist_disabled: {watchlist_id}")
