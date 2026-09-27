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
from govcon.models import Opportunity, User
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
enrich_app = typer.Typer(help="Attachment download and AI analysis.")
prompts_app = typer.Typer(help="Prompt registry administration.")
decision_app = typer.Typer(help="JEV decision bundles and package generation.")
review_app = typer.Typer(help="Collaborative review workspace operations.")
compliance_app = typer.Typer(help="High-reliability compliance matrix, validation, and pre-flight.")
proposal_app = typer.Typer(help="Post-approval proposal generation and final approval (Phase 11).")
submission_app = typer.Typer(help="Submission package generation and tracking (Phase 11).")
mcp_app = typer.Typer(help="Model Context Protocol server (Phase 12).")
embed_app = typer.Typer(help="Embedding generation and semantic search (Phase 13).")
semantic_app = typer.Typer(help="Semantic recommendations (Phase 13).")
web_app = typer.Typer(help="Web UI server (Phase 14).")
jobs_app = typer.Typer(help="Scheduler job chains (Phase 17).")
scheduler_app = typer.Typer(help="APScheduler daemon (Phase 17).")
app.add_typer(db_app, name="db")
app.add_typer(users_app, name="users")
app.add_typer(ingest_app, name="ingest")
app.add_typer(match_app, name="match")
app.add_typer(watchlist_app, name="watchlist")
app.add_typer(alerts_app, name="alerts")
app.add_typer(awards_app, name="awards")
app.add_typer(vendors_app, name="vendors")
app.add_typer(contacts_app, name="contacts")
app.add_typer(enrich_app, name="enrich")
app.add_typer(prompts_app, name="prompts")
app.add_typer(decision_app, name="decision")
app.add_typer(review_app, name="review")
app.add_typer(compliance_app, name="compliance")
app.add_typer(proposal_app, name="proposal")
app.add_typer(submission_app, name="submission")
app.add_typer(mcp_app, name="mcp")
app.add_typer(embed_app, name="embed")
app.add_typer(semantic_app, name="semantic")
app.add_typer(web_app, name="web")
app.add_typer(jobs_app, name="jobs")
app.add_typer(scheduler_app, name="scheduler")


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
    """Print DB connectivity, schema revision, row counts, and last job run results."""
    from sqlalchemy import func, text

    try:
        settings = _settings()
        engine = make_engine(settings)
    except ConfigError as exc:
        _fail_config(exc)
        return
    ok, revision = check_connectivity(engine)
    if not ok:
        typer.echo("database_connectivity: failed")
        typer.echo("schema_revision: unavailable")
        raise typer.Exit(code=1)

    typer.echo("database_connectivity: ok")
    typer.echo(f"schema_revision: {revision}")

    try:
        from govcon.models import IngestionRun, Opportunity, SchedulerJobRun
        from govcon.scheduler.chains import CHAIN_DEFINITIONS

        with session_scope(settings) as db:
            opp_count = db.scalar(select(func.count()).select_from(Opportunity)) or 0
            opp_open = db.scalar(
                select(func.count()).select_from(Opportunity).where(Opportunity.status == "open")
            ) or 0
            typer.echo(f"opportunities_total: {opp_count}")
            typer.echo(f"opportunities_open: {opp_open}")

            typer.echo("")
            typer.echo("--- last job runs ---")
            for chain_name in CHAIN_DEFINITIONS:
                last_run = db.scalars(
                    select(SchedulerJobRun)
                    .where(SchedulerJobRun.chain_name == chain_name)
                    .order_by(SchedulerJobRun.started_at.desc())
                    .limit(1)
                ).first()
                if last_run is None:
                    typer.echo(f"{chain_name}: never_run")
                else:
                    ts = last_run.started_at.strftime("%Y-%m-%d %H:%M UTC") if last_run.started_at else "?"
                    counts = ""
                    if last_run.row_counts:
                        totals = {}
                        for step_counts in last_run.row_counts.values():
                            for k, v in step_counts.items():
                                totals[k] = totals.get(k, 0) + (v or 0)
                        counts = " " + " ".join(f"{k}={v}" for k, v in totals.items() if v)
                    typer.echo(f"{chain_name}: {last_run.status} at {ts}{counts}")
    except Exception as exc:
        typer.echo(f"status_detail_error: {exc}", err=True)


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


# ── Enrichment CLI ──


@enrich_app.command("download")
def enrich_download(
    opportunity_id: int = typer.Option(..., help="Stored opportunity id."),
) -> None:
    """Download and extract text from attachments for an opportunity."""
    from govcon.enrich.attachments import download_attachments

    try:
        settings = _settings()
        settings.require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    with session_scope() as session:
        opp = session.get(Opportunity, opportunity_id)
        if opp is None:
            typer.echo("opportunity not found", err=True)
            raise typer.Exit(code=2)
        files = download_attachments(session, opp, settings=settings)
    typer.echo(f"files_downloaded: {len(files)}")
    for f in files:
        typer.echo(f"file_id: {f.id}")
        typer.echo(f"filename: {f.filename}")
        typer.echo(f"sha256: {f.sha256}")
        typer.echo(f"extraction_status: {f.extraction_status}")
        if f.extraction_error:
            typer.echo(f"extraction_error: {f.extraction_error}")
        typer.echo("---")


@enrich_app.command("analyze")
def enrich_analyze(
    opportunity_id: int = typer.Option(..., help="Stored opportunity id."),
    force: bool = typer.Option(False, help="Re-run even if an analysis exists."),
) -> None:
    """Run structured solicitation analysis on an opportunity."""
    from govcon.enrich.summarize import run_solicitation_analysis

    try:
        settings = _settings()
        settings.require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    with session_scope() as session:
        opp = session.get(Opportunity, opportunity_id)
        if opp is None:
            typer.echo("opportunity not found", err=True)
            raise typer.Exit(code=2)
        analysis = run_solicitation_analysis(session, opp, settings=settings, force=force)
    if analysis is None:
        typer.echo("analysis_skipped: true")
        typer.echo(
            "reason: no extracted files, no AI provider, or analysis already exists"
        )
    else:
        import json as _json

        typer.echo(f"analysis_id: {analysis.id}")
        typer.echo(f"provider: {analysis.provider}")
        typer.echo(f"model: {analysis.model}")
        typer.echo(f"prompt_name: {analysis.prompt_name}")
        typer.echo(f"prompt_version: {analysis.prompt_version}")
        typer.echo(f"schema_version: {analysis.schema_version}")
        typer.echo(f"latency_ms: {analysis.latency_ms}")
        typer.echo("output_json:")
        typer.echo(_json.dumps(analysis.output_json, indent=2))


@enrich_app.command("process")
def enrich_process(
    opportunity_id: int = typer.Option(..., help="Stored opportunity id."),
    force: bool = typer.Option(False, help="Re-run analysis even if one exists."),
) -> None:
    """Download attachments, extract text, and run AI analysis end to end."""
    from govcon.enrich.attachments import download_attachments
    from govcon.enrich.summarize import run_solicitation_analysis

    try:
        settings = _settings()
        settings.require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    with session_scope() as session:
        opp = session.get(Opportunity, opportunity_id)
        if opp is None:
            typer.echo("opportunity not found", err=True)
            raise typer.Exit(code=2)
        files = download_attachments(session, opp, settings=settings)
        typer.echo(f"files_downloaded: {len(files)}")
        analysis = run_solicitation_analysis(session, opp, settings=settings, force=force)
    if analysis:
        typer.echo(f"analysis_id: {analysis.id}")
        typer.echo(f"schema_version: {analysis.schema_version}")
    else:
        typer.echo("analysis_skipped: true")


@enrich_app.command("ingest-file")
def enrich_ingest_file(
    opportunity_id: int = typer.Option(..., help="Stored opportunity id."),
    file_path: str = typer.Option(..., "--file", help="Local file path."),
) -> None:
    """Process a local file: compute SHA-256, extract text, and persist."""
    from pathlib import Path as _Path

    from govcon.enrich.attachments import process_local_file

    try:
        settings = _settings()
        settings.require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    p = _Path(file_path)
    if not p.exists():
        typer.echo(f"file not found: {file_path}", err=True)
        raise typer.Exit(code=2)
    with session_scope() as session:
        opp = session.get(Opportunity, opportunity_id)
        if opp is None:
            typer.echo("opportunity not found", err=True)
            raise typer.Exit(code=2)
        sf = process_local_file(session, opp, p)
    typer.echo(f"file_id: {sf.id}")
    typer.echo(f"filename: {sf.filename}")
    typer.echo(f"sha256: {sf.sha256}")
    typer.echo(f"extraction_status: {sf.extraction_status}")
    if sf.extraction_error:
        typer.echo(f"extraction_error: {sf.extraction_error}")


# ── Prompt registry CLI ──


@prompts_app.command("sync")
def prompts_sync() -> None:
    """Sync source-controlled prompts to the database registry."""
    from govcon.prompting.registry import sync_prompts

    try:
        settings = _settings()
        settings.require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    prompt_root = settings.resolved_prompt_root()
    with session_scope() as session:
        synced = sync_prompts(session, prompt_root)
    for name, versions in synced.items():
        typer.echo(f"synced: {name} [{', '.join(versions)}]")
    typer.echo(f"total_prompts_synced: {sum(len(v) for v in synced.values())}")


@prompts_app.command("activate")
def prompts_activate(
    prompt_name: str = typer.Option(..., help="Prompt name."),
    version: str = typer.Option(..., help="Version to activate."),
) -> None:
    """Activate a prompt version; safety-critical prompts must pass their regression gate."""
    from govcon.prompting.registry import PromptActivationBlocked, activate_prompt

    try:
        settings = _settings()
        settings.require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    try:
        with session_scope() as session:
            result = activate_prompt(session, prompt_name, version, prompt_root=settings.resolved_prompt_root(), settings=settings)
    except PromptActivationBlocked as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"activated: {prompt_name}@{version} (previous: {result['previous_version'] or 'none'})")
    typer.echo(f"gate_required: {result['gate']['required']} gate_passed: {result['gate']['passed']}")


@prompts_app.command("rollback")
def prompts_rollback(prompt_name: str = typer.Option(..., help="Prompt name.")) -> None:
    """Restore the version that was active before the latest activation."""
    from govcon.prompting.registry import rollback_prompt

    try:
        settings = _settings()
        settings.require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    try:
        with session_scope() as session:
            result = rollback_prompt(session, prompt_name)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"rolled_back: {prompt_name} {result['rolled_back_from']} -> {result['active_version']}")


# ── Decision engine CLI ──


def _echo_decision_run(run) -> None:
    typer.echo(f"decision_run_id: {run.id}")
    typer.echo(f"bundle: {run.bundle_name}@{run.bundle_version}")
    typer.echo(f"provider: {run.provider}")
    typer.echo(f"model: {run.model or ''}")
    typer.echo(f"decision_spec: {run.decision_spec_name or ''}")
    typer.echo(f"decision_spec_hash: {run.decision_spec_hash or ''}")
    typer.echo(f"schema_version: {run.schema_version or ''}")
    typer.echo(f"input_state_hash: {run.input_state_hash}")
    typer.echo(f"confidence: {run.confidence if run.confidence is not None else ''}")
    typer.echo(f"latency_ms: {run.latency_ms if run.latency_ms is not None else ''}")
    typer.echo(f"created_at: {run.created_at.isoformat() if run.created_at else ''}")


@decision_app.command("run-bundle")
def decision_run_bundle(
    opportunity_id: int = typer.Option(..., help="Stored opportunity id."),
    bundle: str = typer.Option(..., help="Bundle name, e.g. bid_decision."),
    allow_llm_fallback: bool = typer.Option(
        False,
        help="Allow LLM fallback when primary provider is unavailable.",
    ),
) -> None:
    """Run one decision bundle and persist a decision_runs row."""
    import json as _json

    from govcon.decision.engine import run_decision_bundle

    try:
        settings = _settings()
        settings.require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    try:
        with session_scope(settings) as session:
            execution = run_decision_bundle(
                session,
                opportunity_id=opportunity_id,
                bundle_name=bundle,
                settings=settings,
                allow_llm_fallback=allow_llm_fallback,
            )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    _echo_decision_run(execution.run)
    if execution.hard_rule_findings:
        typer.echo("hard_rule_findings:")
        for item in execution.hard_rule_findings:
            typer.echo(f"- {item}")
    typer.echo("result_json:")
    typer.echo(_json.dumps(execution.result, indent=2))


@decision_app.command("run-package")
def decision_run_package(
    opportunity_id: int = typer.Option(..., help="Stored opportunity id."),
    allow_llm_fallback: bool = typer.Option(
        False,
        help="Allow LLM fallback when primary provider is unavailable.",
    ),
) -> None:
    """Run Phase 8 bundle set and persist an AI decision package."""
    import json as _json

    from govcon.decision.engine import run_preliminary_decision_package

    try:
        settings = _settings()
        settings.require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    with session_scope(settings) as session:
        package = run_preliminary_decision_package(
            session,
            opportunity_id=opportunity_id,
            settings=settings,
            allow_llm_fallback=allow_llm_fallback,
        )
    typer.echo(f"analysis_id: {package.analysis.id}")
    typer.echo(f"bid_decision_id: {package.bid_decision.id}")
    typer.echo(f"recommendation: {package.bid_decision.recommendation}")
    typer.echo("bundle_runs:")
    for run in package.bundle_runs:
        typer.echo(
            f"- {run.bundle_name}: run_id={run.run.id} provider={run.provider} "
            f"model={run.model or ''} confidence={run.confidence if run.confidence is not None else ''}"
        )
    typer.echo("decision_package_json:")
    typer.echo(_json.dumps(package.package_output, indent=2))


@decision_app.command("runs")
def decision_runs(
    opportunity_id: int = typer.Option(..., help="Stored opportunity id."),
    bundle: str | None = typer.Option(None, help="Optional bundle filter."),
    limit: int = typer.Option(25, min=1, help="Maximum rows to print."),
) -> None:
    """List persisted decision runs with provider/model/spec metadata."""
    from govcon.decision.engine import list_decision_runs

    try:
        settings = _settings()
        settings.require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return
    with session_scope(settings) as session:
        rows = list_decision_runs(session, opportunity_id=opportunity_id, bundle_name=bundle, limit=limit)
    typer.echo(f"count: {len(rows)}")
    for row in rows:
        _echo_decision_run(row)
        typer.echo("---")


# ── Collaborative review CLI ──


def _review_actor(session, email: str) -> User:
    user = session.scalar(
        select(User).where(
            User.email == email.strip().lower(),
            User.is_active.is_(True),
        )
    )
    if user is None:
        raise ValueError(f"no active user {email}")
    return user


@review_app.command("assign")
def review_assign(
    opportunity_id: int = typer.Option(..., help="Stored opportunity id."),
    user_id: int = typer.Option(..., help="Reviewer user id."),
    assignment_role: str = typer.Option("reviewer", help="Assignment role label."),
    actor_email: str | None = typer.Option(None, help="Optional actor email for audit attribution."),
) -> None:
    """Assign or re-assign a reviewer to an opportunity."""
    from govcon.collaboration.assignments import assign_reviewer
    from govcon.collaboration.review_sessions import recalculate_quorum

    settings = _compliance_settings()
    if settings is None:
        return
    try:
        with session_scope(settings) as session:
            actor = _review_actor(session, actor_email) if actor_email else None
            row = assign_reviewer(
                session,
                opportunity_id=opportunity_id,
                user_id=user_id,
                assignment_role=assignment_role,
                actor_user_id=actor.id if actor else None,
            )
            quorum = recalculate_quorum(session, opportunity_id=opportunity_id)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"assignment_id: {row.id} status={row.status} quorum={quorum.completed_review_count}/{quorum.required_review_count}"
    )


@review_app.command("comment")
def review_comment(
    opportunity_id: int = typer.Option(..., help="Stored opportunity id."),
    user_id: int = typer.Option(..., help="Reviewer user id."),
    body: str = typer.Option(..., help="Human comment text."),
    topic: str | None = typer.Option(None, help="Optional concern topic."),
    parent_comment_id: int | None = typer.Option(None, help="Optional parent comment id for threads."),
    recommendation: str | None = typer.Option(None, help="Optional recommendation tied to the comment."),
    no_ai: bool = typer.Option(False, "--no-ai", help="Skip AI validation for this comment."),
) -> None:
    """Append a threaded reviewer comment; preserves original text and optional AI opinion."""
    from govcon.collaboration.comments import add_comment

    settings = _compliance_settings()
    if settings is None:
        return
    try:
        with session_scope(settings) as session:
            row = add_comment(
                session,
                opportunity_id=opportunity_id,
                user_id=user_id,
                body=body,
                topic=topic,
                parent_comment_id=parent_comment_id,
                user_recommendation=recommendation,
                validate_with_ai=not no_ai,
            )
    except (ValueError, RuntimeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"comment_id: {row.id}")
    typer.echo(f"ai_position: {row.ai_position}")
    typer.echo(f"ai_confidence: {row.ai_confidence}")


@review_app.command("complete")
def review_complete(
    opportunity_id: int = typer.Option(..., help="Stored opportunity id."),
    user_id: int = typer.Option(..., help="Reviewer user id."),
    action: str = typer.Option(
        ...,
        help="approve_continue | request_second_review | return_for_ai_analysis | no_bid",
    ),
    recommendation: str | None = typer.Option(None, help="Reviewer recommendation, e.g. bid/no_bid/review."),
    agree_with_ai_assessment: bool | None = typer.Option(
        None,
        help="Explicit agreement when no comment text is added.",
    ),
    second_review_reason: str | None = typer.Option(
        None,
        help="Reason when requesting a second review.",
    ),
) -> None:
    """Mark one reviewer action complete and recompute quorum/consolidation."""
    from govcon.collaboration.review_sessions import complete_assignment

    settings = _compliance_settings()
    if settings is None:
        return
    try:
        with session_scope(settings) as session:
            row = complete_assignment(
                session,
                opportunity_id=opportunity_id,
                user_id=user_id,
                action=action,
                recommendation=recommendation,
                agree_with_ai_assessment=agree_with_ai_assessment,
                second_review_reason=second_review_reason,
            )
    except (ValueError, RuntimeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"assignment_id: {row.id} status={row.status}")


@review_app.command("context")
def review_context(
    opportunity_id: int = typer.Option(..., help="Stored opportunity id."),
    user_id: int | None = typer.Option(None, help="Optional current user id (starts assigned review)."),
) -> None:
    """Show the collaborative review workspace state and approval context."""
    import json as _json

    from govcon.collaboration.review_sessions import review_workspace

    settings = _compliance_settings()
    if settings is None:
        return
    with session_scope(settings) as session:
        data = review_workspace(session, opportunity_id=opportunity_id, user_id=user_id)
    typer.echo(_json.dumps(data, indent=2, default=str))


@review_app.command("approve")
def review_approve(
    opportunity_id: int = typer.Option(..., help="Stored opportunity id."),
    actor_email: str = typer.Option(..., help="Approver/owner email."),
    action: str = typer.Option(..., help="approve_to_bid | return_for_review | no_bid"),
    expected_version: int | None = typer.Option(None, help="Optional review-session optimistic version."),
    override_reason: str | None = typer.Option(
        None,
        help="Required when approving without quorum; must be explicit.",
    ),
) -> None:
    """Finalize the human approval gate decision after collaborative review."""
    from govcon.collaboration.review_sessions import finalize_approval

    settings = _compliance_settings()
    if settings is None:
        return
    try:
        with session_scope(settings) as session:
            actor = _review_actor(session, actor_email)
            row = finalize_approval(
                session,
                opportunity_id=opportunity_id,
                actor=actor,
                action=action,
                expected_version=expected_version,
                override_reason=override_reason,
            )
    except (ValueError, RuntimeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"review_session_id: {row.id} status={row.status} final_approval_status={row.final_approval_status}"
    )


@review_app.command("reopen-for-amendment")
def review_reopen_for_amendment(
    opportunity_id: int = typer.Option(..., help="Stored opportunity id."),
    actor_email: str | None = typer.Option(None, help="Optional actor email for audit attribution."),
) -> None:
    """Consume Phase 9 material-amendment reopen flag and reopen completed reviews."""
    from govcon.collaboration.review_sessions import apply_material_amendment_reopen

    settings = _compliance_settings()
    if settings is None:
        return
    with session_scope(settings) as session:
        actor = _review_actor(session, actor_email) if actor_email else None
        changed = apply_material_amendment_reopen(
            session, opportunity_id=opportunity_id, actor=actor
        )
    typer.echo(f"reopened: {'yes' if changed else 'no'}")


# ── Compliance CLI ──


def _compliance_settings() -> Settings | None:
    try:
        settings = _settings()
        settings.require_database_url()
        return settings
    except ConfigError as exc:
        _fail_config(exc)
        return None


def _load_json_file(path: str | None) -> dict | None:
    import json as _json
    from pathlib import Path as _Path

    if not path:
        return None
    return _json.loads(_Path(path).read_text(encoding="utf-8"))


def _actor(session, email: str) -> User:
    user = session.scalar(select(User).where(User.email == email.strip().lower(), User.is_active.is_(True)))
    if user is None:
        typer.echo(f"no active user {email}", err=True)
        raise typer.Exit(code=2)
    return user


@compliance_app.command("run")
def compliance_run(
    opportunity_id: int = typer.Option(..., help="Stored opportunity id."),
    no_ai: bool = typer.Option(False, "--no-ai", help="Deterministic stages only (run is marked incomplete)."),
    force: bool = typer.Option(False, help="Re-extract even when the source inventory is unchanged."),
    company_facts: str | None = typer.Option(None, help="JSON file of approved company facts (default COMPANY_FACTS_PATH)."),
    supplier: str | None = typer.Option(None, help="JSON file of supplier facts (lead_time_days, transit_days)."),
    package: str | None = typer.Option(None, help="JSON submission package manifest."),
) -> None:
    """Run inventory → dual extraction → reconciliation → validators → red team → JEV routing."""
    from govcon.compliance.deterministic import SubmissionPackage
    from govcon.compliance.metrics import format_coverage
    from govcon.compliance.pipeline import run_compliance_pipeline

    settings = _compliance_settings()
    if settings is None:
        return
    package_data = _load_json_file(package)
    with session_scope(settings) as session:
        result = run_compliance_pipeline(
            session,
            opportunity_id,
            use_ai=not no_ai,
            force=force,
            company_facts=_load_json_file(company_facts),
            supplier=_load_json_file(supplier),
            package=SubmissionPackage.from_dict(package_data) if package_data else None,
            settings=settings,
        )
    typer.echo(f"status: {result['status']}")
    typer.echo(f"matrix_run_id: {result['matrix_run_id']}")
    for warning in result["warnings"]:
        typer.echo(f"WARNING [{warning.get('code')}]: {warning.get('message')}")
    for line in format_coverage(result["counts"]):
        typer.echo(line)


@compliance_app.command("inventory")
def compliance_inventory(opportunity_id: int = typer.Option(..., help="Stored opportunity id.")) -> None:
    """Build and print the document inventory with warnings."""
    from govcon.compliance.inventory import build_document_inventory

    settings = _compliance_settings()
    if settings is None:
        return
    with session_scope(settings) as session:
        inventory, run = build_document_inventory(session, opportunity_id)
        typer.echo(f"inventory_run_id: {run.id} status: {run.status}")
        for doc in inventory.documents:
            m = doc.manifest()
            typer.echo(
                f"- file {m['file_id']} {m['filename']} type={m['document_type']} sha256={(m['sha256'] or '')[:12]} "
                f"pages={m['page_count']} text={m['text_extraction_status']} tables={m['table_extraction_status']} ocr_needed={m['ocr_needed']}"
            )
        for warning in inventory.warnings:
            typer.echo(f"WARNING {warning.severity} [{warning.code}]{' BLOCKING' if warning.blocking else ''}: {warning.message}")


@compliance_app.command("matrix")
def compliance_matrix_cmd(
    opportunity_id: int = typer.Option(..., help="Stored opportunity id."),
    filter_: list[str] = typer.Option([], "--filter", help="critical_only, missing, unknown, needs_review, stale, submission_blockers, recently_changed_by_amendment, not_independently_confirmed"),
    as_json: bool = typer.Option(False, "--json", help="Print JSON rows."),
) -> None:
    """Print the source-backed compliance matrix."""
    import json as _json

    from govcon.compliance.matrix import compliance_matrix

    settings = _compliance_settings()
    if settings is None:
        return
    with session_scope(settings) as session:
        try:
            rows = compliance_matrix(session, opportunity_id, filters=filter_)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
    if as_json:
        typer.echo(_json.dumps(rows, indent=2, default=str))
        return
    typer.echo(f"count: {len(rows)}")
    for row in rows:
        src = row["source"]
        typer.echo(
            f"[{row['requirement_id']}] {row['status'].upper()} sev={row['severity']} mandatory={row['mandatory']} "
            f"confirmed={row['independently_confirmed']} blocking={row['blocking']} stale={row['amendment_freshness']['stale']}"
        )
        typer.echo(f"    {row['requirement'][:160]}")
        typer.echo(f"    source: file {src['file_id']} ({src['filename']}) page {src['page']} section {src['section']}")
        typer.echo(f"    evidence: {len(row['evidence'])} rows; validators: {', '.join(v['validator'] + '=' + v['status'] for v in row['validator_output']) or 'none'}")
        if row["status_reason"]:
            typer.echo(f"    reason: {row['status_reason']}")


@compliance_app.command("coverage")
def compliance_coverage(opportunity_id: int = typer.Option(..., help="Stored opportunity id.")) -> None:
    """Print coverage counts by status and category."""
    from govcon.compliance.metrics import format_coverage, record_matrix_run

    settings = _compliance_settings()
    if settings is None:
        return
    with session_scope(settings) as session:
        counts, run_id = record_matrix_run(session, opportunity_id)
    typer.echo(f"matrix_run_id: {run_id}")
    for line in format_coverage(counts):
        typer.echo(line)


@compliance_app.command("findings")
def compliance_findings(
    opportunity_id: int = typer.Option(..., help="Stored opportunity id."),
    blocking: bool = typer.Option(False, help="Only submission blockers."),
) -> None:
    """List open compliance findings (red team, conflicts, inventory, pre-flight)."""
    from govcon.compliance.matrix import open_findings

    settings = _compliance_settings()
    if settings is None:
        return
    with session_scope(settings) as session:
        rows = open_findings(session, opportunity_id, blocking_only=blocking)
        typer.echo(f"count: {len(rows)}")
        for f in rows:
            typer.echo(f"[{f.id}] {f.severity} {f.finding_type} ({f.certainty}, {f.detected_by}){' BLOCKING' if f.blocks_submission else ''}: {f.description}")


@compliance_app.command("clauses-seed")
def compliance_clauses_seed() -> None:
    """Upsert the verified clause-library seed."""
    from govcon.compliance.clauses import seed_clause_library

    settings = _compliance_settings()
    if settings is None:
        return
    with session_scope(settings) as session:
        count = seed_clause_library(session)
    typer.echo(f"clauses_upserted: {count}")


@compliance_app.command("evidence-add")
def compliance_evidence_add(
    requirement_id: int = typer.Option(...),
    evidence_type: str = typer.Option(..., help="e.g. supplier_quote, company_registration, signed_form"),
    method: str = typer.Option(..., help="verification method, e.g. supplier_document, company_record"),
    status: str = typer.Option("unverified", help="unverified | verified | conflicting | insufficient"),
    description: str = typer.Option(...),
    source_file_id: int | None = typer.Option(None),
    page: int | None = typer.Option(None),
    quote: str | None = typer.Option(None),
) -> None:
    """Record evidence for a requirement (does not change its status until validation runs)."""
    from govcon.compliance.matrix import add_evidence

    settings = _compliance_settings()
    if settings is None:
        return
    try:
        with session_scope(settings) as session:
            row = add_evidence(
                session, requirement_id=requirement_id, evidence_type=evidence_type, verification_method=method,
                verification_status=status, description=description, source_file_id=source_file_id, source_page=page, source_quote=quote,
            )
            typer.echo(f"evidence_id: {row.id}")
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc


@compliance_app.command("override")
def compliance_override(
    requirement_id: int = typer.Option(...),
    status: str = typer.Option(..., help="satisfied | missing | unknown | needs_review | not_applicable"),
    reason: str = typer.Option(..., help="Explicit reason (recorded in audit history)."),
    actor_email: str = typer.Option(..., help="Authorized owner/approver email."),
    expected_version: int = typer.Option(..., help="Requirement version you reviewed (optimistic concurrency)."),
    acknowledge_deterministic_failure: bool = typer.Option(False, help="Required to override a failed deterministic validator."),
) -> None:
    """Authorized human override of one requirement's compliance state."""
    from govcon.collaboration.users import PermissionDenied
    from govcon.compliance.matrix import ComplianceInvariantError, override_requirement
    from govcon.concurrency import StaleRecordError

    settings = _compliance_settings()
    if settings is None:
        return
    try:
        with session_scope(settings) as session:
            req = override_requirement(
                session, requirement_id=requirement_id, status=status, actor=_actor(session, actor_email), reason=reason,
                expected_version=expected_version, acknowledge_deterministic_failure=acknowledge_deterministic_failure,
            )
            typer.echo(f"requirement {req.id}: {req.status} (version {req.version})")
    except (PermissionDenied, ComplianceInvariantError, StaleRecordError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc


@compliance_app.command("proposal-coverage")
def compliance_proposal_coverage(
    opportunity_id: int = typer.Option(...),
    proposal_version_id: int = typer.Option(...),
    ai: bool = typer.Option(False, help="Also run the proposal_coverage AI auditor."),
) -> None:
    """Map response requirements to a selected proposal version; gaps become blocking findings."""
    from govcon.compliance.proposal_coverage import check_proposal_coverage
    from govcon.compliance.validator import run_validation

    settings = _compliance_settings()
    if settings is None:
        return
    with session_scope(settings) as session:
        result = check_proposal_coverage(session, opportunity_id, proposal_version_id, use_ai=ai, settings=settings)
        run_validation(session, opportunity_id, use_ai=False, settings=settings)
    typer.echo(f"coverage_run_id: {result['run_id']} summary: {result['summary']}")
    for rid, item in result["results"].items():
        typer.echo(f"- requirement {rid}: {item['coverage_status']} section={item['section_key']} {item['issue'] or ''}")


@compliance_app.command("preflight")
def compliance_preflight(
    opportunity_id: int = typer.Option(...),
    package: str = typer.Option(..., help="JSON submission package manifest."),
    submission_id: int | None = typer.Option(None),
    ai: bool = typer.Option(False, help="Also run the AI pre-flight reviewer."),
) -> None:
    """Final submission-package pre-flight. Exit 1 unless every item is green."""
    from govcon.compliance.deterministic import SubmissionPackage
    from govcon.compliance.submission_preflight import run_submission_preflight

    settings = _compliance_settings()
    if settings is None:
        return
    with session_scope(settings) as session:
        result = run_submission_preflight(
            session, opportunity_id, SubmissionPackage.from_dict(_load_json_file(package) or {}),
            submission_id=submission_id, use_ai=ai, settings=settings,
        )
    typer.echo(f"preflight_run_id: {result['run_id']} status: {result['status']}")
    for item in result["items"]:
        typer.echo(f"- {item['status'].upper():<14} {item['check']}: {item['reason']}")
    if not result["ready"]:
        raise typer.Exit(code=1)


@compliance_app.command("ready")
def compliance_ready(
    opportunity_id: int = typer.Option(...),
    actor_email: str = typer.Option(..., help="Approver/owner email."),
    override_reason: str | None = typer.Option(None, help="Explicit override reason when blockers remain (audited)."),
) -> None:
    """Move the pursuit to ready_to_submit only when no compliance blocker remains."""
    from govcon.collaboration.users import PermissionDenied
    from govcon.compliance.submission_preflight import ReadinessBlocked, move_to_ready_to_submit

    settings = _compliance_settings()
    if settings is None:
        return
    try:
        with session_scope(settings) as session:
            pursuit = move_to_ready_to_submit(session, opportunity_id, actor=_actor(session, actor_email), override_reason=override_reason)
            typer.echo(f"pursuit {pursuit.id}: {pursuit.stage}")
    except ReadinessBlocked as exc:
        typer.echo("NOT READY:", err=True)
        for blocker in exc.blockers:
            typer.echo(f"- {blocker['description']}", err=True)
        raise typer.Exit(code=1) from exc
    except (PermissionDenied, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc


@compliance_app.command("benchmark")
def compliance_benchmark(
    fixtures: str | None = typer.Option(None, help="Benchmark root (default tests/fixtures/compliance)."),
    live: bool = typer.Option(False, help="Re-run AI passes against the configured provider instead of recorded outputs."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Run the compliance benchmark; exit 1 when the release gate fails."""
    import json as _json
    from pathlib import Path as _Path

    from govcon.compliance.regression import default_fixture_root, run_benchmark_suite

    settings = _settings()
    suite = run_benchmark_suite(_Path(fixtures) if fixtures else default_fixture_root(), live=live, settings=settings)
    if as_json:
        typer.echo(_json.dumps(suite.as_dict(), indent=2, default=str))
    else:
        for case in suite.cases:
            typer.echo(f"{case.case_id}: {case.metrics}")
        typer.echo(f"aggregate: {suite.aggregate}")
        typer.echo(f"gate: {'PASS' if suite.gate.passed else 'FAIL'}")
        for failure in suite.gate.failures:
            typer.echo(f"- {failure}")
    if not suite.gate.passed:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Phase 11 — Proposal commands
# ---------------------------------------------------------------------------

@proposal_app.command("generate")
def proposal_generate(
    opportunity_id: int = typer.Option(...),
    actor_email: str = typer.Option(..., help="Authorized user email."),
    skip_ai: bool = typer.Option(False, help="Use placeholder draft (no AI key required)."),
) -> None:
    """Generate an AI proposal draft for an approved-to-bid opportunity."""
    from govcon.proposals.service import generate_proposal

    settings = _settings()
    try:
        with session_scope(settings) as session:
            actor = _actor(session, actor_email)
            result = generate_proposal(session, opportunity_id=opportunity_id, actor=actor, skip_ai=skip_ai, settings=settings)
        typer.echo(f"proposal_id: {result['proposal_id']}  version_id: {result['version_id']}  sections: {result['section_count']}")
        if result.get("global_blockers"):
            typer.echo("Global blockers:", err=True)
            for b in result["global_blockers"]:
                typer.echo(f"  {b}", err=True)
    except (ValueError, RuntimeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@proposal_app.command("status")
def proposal_status(
    opportunity_id: int = typer.Option(...),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show the proposal workspace for the final approver."""
    import json as _json
    from govcon.proposals.service import get_proposal_workspace

    settings = _settings()
    with session_scope(settings) as session:
        ws = get_proposal_workspace(session, opportunity_id=opportunity_id)
    if as_json:
        typer.echo(_json.dumps(ws, indent=2, default=str))
    else:
        typer.echo(f"readiness: {ws['readiness']}")
        typer.echo(f"proposal_status: {ws['proposal_status']}")
        typer.echo(f"version: v{ws['current_version_number']}")
        typer.echo(f"mandatory {ws['mandatory_satisfied']}/{ws['mandatory_total']} satisfied")
        typer.echo(f"critical unresolved: {ws['critical_unresolved']}")
        if ws["blocking_issues"]:
            for issue in ws["blocking_issues"]:
                typer.echo(f"BLOCKER: {issue}", err=True)


@proposal_app.command("red-team")
def proposal_red_team(
    opportunity_id: int = typer.Option(...),
    proposal_version_id: int = typer.Option(...),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Run the red-team AI reviewer against a proposal version."""
    import json as _json
    from govcon.proposals.ai_review import run_proposal_red_team
    from govcon.ai.structured import StructuredCallError

    settings = _settings()
    try:
        with session_scope(settings) as session:
            result = run_proposal_red_team(
                session,
                opportunity_id=opportunity_id,
                proposal_version_id=proposal_version_id,
                settings=settings,
            )
        if as_json:
            typer.echo(_json.dumps(result, indent=2, default=str))
        else:
            typer.echo(f"assessment: {result['overall_assessment']}  critical: {result['critical_count']}  major: {result['major_count']}  minor: {result['minor_count']}")
            for f in result["result"].get("findings", []):
                typer.echo(f"  [{f['severity'].upper()}] req={f.get('requirement_id')} sec={f.get('proposal_section')}: {f['description'][:100]}")
    except StructuredCallError as exc:
        typer.echo(f"AI unavailable: {exc.reason}: {exc.detail}", err=True)
        raise typer.Exit(code=1) from exc


@proposal_app.command("approve")
def proposal_approve(
    opportunity_id: int = typer.Option(...),
    actor_email: str = typer.Option(..., help="Authorized approver email."),
    override_reason: str | None = typer.Option(None, help="Override reason when blockers remain."),
) -> None:
    """APPROVE FOR SUBMISSION: mark proposal final-approved and advance pursuit to ready_to_submit."""
    from govcon.proposals.service import finalize_proposal
    from govcon.compliance.submission_preflight import ReadinessBlocked
    from govcon.collaboration.users import PermissionDenied

    settings = _settings()
    try:
        with session_scope(settings) as session:
            actor = _actor(session, actor_email)
            result = finalize_proposal(session, opportunity_id=opportunity_id, action="APPROVE_FOR_SUBMISSION", actor=actor, override_reason=override_reason)
        typer.echo(f"proposal {result['proposal_id']} → {result['status']}  pursuit → {result['pursuit_stage']}")
    except ReadinessBlocked as exc:
        typer.echo(f"Blocked by {len(exc.blockers)} issue(s). Use --override-reason to bypass.", err=True)
        raise typer.Exit(code=1) from exc
    except (PermissionDenied, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc


@proposal_app.command("return")
def proposal_return(
    opportunity_id: int = typer.Option(...),
    actor_email: str = typer.Option(..., help="Authorized approver email."),
) -> None:
    """RETURN FOR FIX: return the proposal for additional work."""
    from govcon.proposals.service import finalize_proposal
    from govcon.collaboration.users import PermissionDenied

    settings = _settings()
    try:
        with session_scope(settings) as session:
            actor = _actor(session, actor_email)
            result = finalize_proposal(session, opportunity_id=opportunity_id, action="RETURN_FOR_FIX", actor=actor)
        typer.echo(f"proposal {result['proposal_id']} → {result['status']}")
    except (PermissionDenied, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc


@proposal_app.command("cancel")
def proposal_cancel(
    opportunity_id: int = typer.Option(...),
    actor_email: str = typer.Option(..., help="Authorized approver email."),
) -> None:
    """CANCEL BID: cancel the pursuit."""
    from govcon.proposals.service import finalize_proposal
    from govcon.collaboration.users import PermissionDenied

    settings = _settings()
    try:
        with session_scope(settings) as session:
            actor = _actor(session, actor_email)
            result = finalize_proposal(session, opportunity_id=opportunity_id, action="CANCEL_BID", actor=actor)
        typer.echo(f"proposal {result['proposal_id']} → {result['status']}  pursuit → {result['pursuit_stage']}")
    except (PermissionDenied, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc


@proposal_app.command("export")
def proposal_export(
    opportunity_id: int = typer.Option(...),
    format_: str = typer.Option("zip", "--format", help="docx | xlsx | zip"),
    output: str | None = typer.Option(None, help="Output file path (default: stdout as bytes or current dir)."),
) -> None:
    """Export the current proposal version (DOCX/XLSX) or the full submission package (ZIP)."""
    import pathlib
    from govcon.models import Proposal
    from sqlalchemy import select as _select

    settings = _settings()
    with session_scope(settings) as session:
        proposal = session.scalars(_select(Proposal).where(Proposal.opportunity_id == opportunity_id)).first()
        if proposal is None:
            typer.echo("No proposal for this opportunity.", err=True)
            raise typer.Exit(code=1)

        if format_.lower() == "docx":
            from govcon.proposals.export import export_proposal_docx
            if proposal.current_version_id is None:
                typer.echo("No proposal version yet.", err=True)
                raise typer.Exit(code=1)
            data = export_proposal_docx(session, proposal_version_id=proposal.current_version_id)
            ext = ".docx"
        elif format_.lower() == "xlsx":
            from govcon.proposals.export import export_coverage_xlsx
            if proposal.current_version_id is None:
                typer.echo("No proposal version yet.", err=True)
                raise typer.Exit(code=1)
            data = export_coverage_xlsx(session, opportunity_id=opportunity_id, proposal_version_id=proposal.current_version_id)
            ext = ".xlsx"
        elif format_.lower() == "zip":
            from govcon.proposals.export import export_submission_zip
            data = export_submission_zip(session, opportunity_id=opportunity_id)
            ext = ".zip"
        else:
            typer.echo(f"Unknown format: {format_!r}. Use docx | xlsx | zip.", err=True)
            raise typer.Exit(code=1)

    out_path = pathlib.Path(output) if output else pathlib.Path(f"opp_{opportunity_id}_proposal{ext}")
    out_path.write_bytes(data)
    typer.echo(f"Wrote {len(data):,} bytes → {out_path}")


# ---------------------------------------------------------------------------
# Phase 11 — Submission commands
# ---------------------------------------------------------------------------

@submission_app.command("package")
def submission_package(
    opportunity_id: int = typer.Option(...),
    actor_email: str | None = typer.Option(None),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Generate or refresh the submission package from solicitation evidence."""
    import json as _json
    from govcon.submissions.service import generate_submission_package

    settings = _settings()
    with session_scope(settings) as session:
        actor = _actor(session, actor_email) if actor_email else None
        result = generate_submission_package(session, opportunity_id=opportunity_id, actor=actor, settings=settings)
    if as_json:
        typer.echo(_json.dumps(result, indent=2, default=str))
    else:
        typer.echo(f"submission_id: {result['submission_id']}  status: {result['status']}")
        typer.echo(f"method: {result['submission_method']}  destination: {result['submission_destination']}")
        typer.echo(f"deadline: {result['deadline']} ({result['deadline_timezone']})")
        typer.echo(f"required_files: {result['required_files']}")
        if result["missing_documents"]:
            for m in result["missing_documents"]:
                typer.echo(f"  MISSING: {m}", err=True)


@submission_app.command("checklist")
def submission_checklist(
    opportunity_id: int = typer.Option(...),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show the final submission checklist."""
    import json as _json
    from govcon.submissions.checklist import generate_final_checklist

    settings = _settings()
    with session_scope(settings) as session:
        result = generate_final_checklist(session, opportunity_id=opportunity_id)
    if as_json:
        typer.echo(_json.dumps(result, indent=2, default=str))
    else:
        typer.echo(f"overall: {result['overall'].upper()}")
        for item in result["items"]:
            flag = "✓" if item["status"] == "ready" else ("✗" if item["status"] == "blocked" else "?")
            typer.echo(f"  [{flag}] {item['label']}: {item['detail']}")


@submission_app.command("instructions")
def submission_instructions(
    opportunity_id: int = typer.Option(...),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show step-by-step submission instructions."""
    import json as _json
    from govcon.submissions.checklist import generate_step_by_step_instructions

    settings = _settings()
    with session_scope(settings) as session:
        steps = generate_step_by_step_instructions(session, opportunity_id=opportunity_id)
    if as_json:
        typer.echo(_json.dumps(steps, indent=2, default=str))
    else:
        for step in steps:
            typer.echo(f"Step {step['step']} [{step['status'].upper()}]: {step['title']}")
            typer.echo(f"  {step['description']}")


@submission_app.command("email-draft")
def submission_email_draft(
    opportunity_id: int = typer.Option(...),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Generate a draft submission email."""
    import json as _json
    from govcon.submissions.email_adapter import draft_submission_email

    settings = _settings()
    with session_scope(settings) as session:
        draft = draft_submission_email(session, opportunity_id=opportunity_id)
    if as_json:
        typer.echo(_json.dumps(draft, indent=2, default=str))
    else:
        typer.echo(f"To: {draft['to']}")
        typer.echo(f"Subject: {draft['subject']}")
        typer.echo(f"Attachments: {draft['attachments']}")
        typer.echo("---")
        typer.echo(draft["body"])
        if draft["notes"]:
            for n in draft["notes"]:
                typer.echo(f"NOTE: {n}", err=True)


@mcp_app.command("serve")
def mcp_serve() -> None:
    """Start the GovCon MCP server on stdio."""
    from govcon.mcp.server import main as run_mcp_server

    run_mcp_server()


@submission_app.command("confirm")
def submission_confirm(
    opportunity_id: int = typer.Option(...),
    actor_email: str = typer.Option(..., help="Authorized approver email."),
    confirmation_number: str | None = typer.Option(None, help="Confirmation number from the portal/email."),
    notes: str | None = typer.Option(None, help="Optional notes about the submission."),
) -> None:
    """Record that the human has submitted and has a confirmation number."""
    from govcon.proposals.service import record_submission_confirmation
    from govcon.collaboration.users import PermissionDenied

    settings = _settings()
    try:
        with session_scope(settings) as session:
            actor = _actor(session, actor_email)
            result = record_submission_confirmation(
                session,
                opportunity_id=opportunity_id,
                confirmation_number=confirmation_number,
                confirmation_notes=notes,
                actor=actor,
            )
        typer.echo(f"submission {result['submission_id']} → {result['status']}  confirmation: {result.get('confirmation_number')}")
    except (PermissionDenied, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc


# ── embed commands (Phase 13) ─────────────────────────────────────────────────


@embed_app.command("run")
def embed_run(
    all_opps: bool = typer.Option(False, "--all", help="Re-embed already-embedded opportunities."),
    batch_size: int = typer.Option(200, "--batch-size", help="Flush interval (rows)."),
) -> None:
    """Generate embeddings for opportunities that have none (or all when --all)."""
    from govcon.enrich.embeddings import get_default_provider, run_embedding_job

    settings = _settings()
    provider = get_default_provider(settings.embedding_model)
    with session_scope(settings) as session:
        stats = run_embedding_job(
            session,
            provider,
            batch_size=batch_size,
            only_missing=not all_opps,
        )
    typer.echo(f"embedded: {stats['embedded']}  skipped: {stats['skipped']}")


@embed_app.command("watchlists")
def embed_watchlists(
    watchlist_id: int | None = typer.Option(None, "--watchlist", help="Rebuild a single watchlist profile."),
) -> None:
    """Build or rebuild watchlist profile embeddings."""
    from govcon.enrich.embeddings import build_watchlist_profiles, get_default_provider

    settings = _settings()
    provider = get_default_provider(settings.embedding_model)
    with session_scope(settings) as session:
        stats = build_watchlist_profiles(session, provider, watchlist_id=watchlist_id)
    typer.echo(f"updated: {stats['updated']}")


# ── semantic commands (Phase 13) ──────────────────────────────────────────────


@semantic_app.command("similar")
def semantic_similar(
    opportunity_id: int = typer.Argument(..., help="Opportunity ID to find similar ones for."),
    limit: int = typer.Option(10, "--limit", help="Maximum results."),
) -> None:
    """Find opportunities semantically similar to a given opportunity."""
    import json

    from govcon.matching.semantic import similar_opportunities as _similar

    settings = _settings()
    with session_scope(settings) as session:
        result = _similar(session, opportunity_id, limit=limit)
    typer.echo(json.dumps(result, indent=2, default=str))


@semantic_app.command("recommendations")
def semantic_recommendations(
    watchlist_id: int | None = typer.Option(None, "--watchlist", help="Watchlist ID for semantic match category."),
    limit: int = typer.Option(10, "--limit", help="Max results per category."),
) -> None:
    """Show recommendations in all five categories for a watchlist."""
    import json

    from govcon.enrich.embeddings import get_default_provider
    from govcon.matching.semantic import all_recommendations

    settings = _settings()
    provider = get_default_provider(settings.embedding_model)
    with session_scope(settings) as session:
        result = all_recommendations(
            session,
            provider,
            watchlist_id=watchlist_id,
            limit=limit,
        )
    typer.echo(json.dumps(result, indent=2, default=str))

# ── jobs commands (Phase 17) ─────────────────────────────────────────────────


@jobs_app.command("list")
def jobs_list() -> None:
    """List all configured scheduler job chains with their schedule and last run status."""
    from sqlalchemy import func

    from govcon.models import SchedulerJobRun
    from govcon.scheduler.chains import CHAIN_DEFINITIONS

    try:
        _settings().require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return

    with session_scope() as db:
        for chain_name, chain_def in CHAIN_DEFINITIONS.items():
            last_run = db.scalars(
                select(SchedulerJobRun)
                .where(SchedulerJobRun.chain_name == chain_name)
                .order_by(SchedulerJobRun.started_at.desc())
                .limit(1)
            ).first()

            if last_run is None:
                last_info = "never_run"
                row_info = ""
            else:
                ts = last_run.started_at.strftime("%Y-%m-%d %H:%M UTC") if last_run.started_at else "?"
                last_info = f"{last_run.status} at {ts}"
                if last_run.row_counts:
                    totals: dict[str, int] = {}
                    for step_counts in last_run.row_counts.values():
                        for k, v in step_counts.items():
                            totals[k] = totals.get(k, 0) + (v or 0)
                    row_info = "  " + " ".join(f"{k}={v}" for k, v in totals.items() if v)
                else:
                    row_info = ""

            typer.echo(f"chain: {chain_name}")
            typer.echo(f"  schedule: {chain_def.cron}")
            typer.echo(f"  steps: {' → '.join(chain_def.steps)}")
            typer.echo(f"  last_run: {last_info}{row_info}")
            if last_run and last_run.failed_step:
                typer.echo(f"  failed_step: {last_run.failed_step}")
                typer.echo(f"  error: {last_run.error or ''}")
            typer.echo("")


@jobs_app.command("run")
def jobs_run(
    job: str = typer.Argument(..., help="Chain name to run (e.g. morning_ingest, usaspending)."),
) -> None:
    """Manually run a named job chain synchronously and print the result."""
    from govcon.scheduler.chains import ALL_CHAIN_NAMES, run_chain

    try:
        settings = _settings()
        settings.require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return

    if job not in ALL_CHAIN_NAMES:
        typer.echo(f"unknown chain: {job!r}. Available: {', '.join(ALL_CHAIN_NAMES)}", err=True)
        raise typer.Exit(code=2)

    typer.echo(f"running chain: {job}")
    result = run_chain(job, settings, trigger="manual")

    typer.echo(f"status: {result.status}")
    typer.echo(f"run_id: {result.run_id}")
    typer.echo(f"steps_completed: {result.steps_completed}")
    if result.failed_step:
        typer.echo(f"failed_step: {result.failed_step}")
        typer.echo(f"error: {result.error}")

    for sr in result.step_results:
        counts = " ".join(f"{k}={v}" for k, v in sr.row_counts().items() if v)
        typer.echo(f"  step={sr.step} status={sr.status} {counts}")
        if sr.extra:
            typer.echo(f"    extra={sr.extra}")

    if result.failed:
        raise typer.Exit(code=1)


# ── scheduler commands (Phase 17) ────────────────────────────────────────────


@scheduler_app.command("start")
def scheduler_start() -> None:
    """Start the APScheduler daemon (blocking). Press Ctrl-C to stop."""
    from govcon.scheduler.runner import start_blocking_scheduler

    try:
        settings = _settings()
        settings.require_database_url()
    except ConfigError as exc:
        _fail_config(exc)
        return

    typer.echo("Starting GovCon scheduler daemon (Ctrl-C to stop)...")
    start_blocking_scheduler(settings)


@web_app.command("serve")
def web_serve(
    host: str | None = typer.Option(None, help="Override bind host (default: from settings)."),
    port: int = typer.Option(8000, help="HTTP port."),
    reload: bool = typer.Option(False, help="Enable auto-reload (dev only)."),
) -> None:
    """Start the GovCon web UI server."""
    import uvicorn

    settings = _settings()
    bind_host = host or settings.web_bind_host
    typer.echo(f"Starting GovCon web UI on http://{bind_host}:{port}")
    uvicorn.run(
        "govcon.web.app:create_app",
        host=bind_host,
        port=port,
        reload=reload,
        factory=True,
    )
