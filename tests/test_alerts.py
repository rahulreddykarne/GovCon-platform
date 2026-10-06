"""Phase 3 alert digest tests."""

from __future__ import annotations

import os
import smtplib
import ssl
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from govcon.alerts.digest import (
    DigestDeliveryError,
    calendar_days_remaining,
    format_estimated_value,
    run_digest,
    send_smtp,
    source_link,
)
from govcon.cli import app
from govcon.config import Settings
from govcon.matching.engine import run_matching
from govcon.matching.watchlists import create_watchlist
from govcon.models import (
    AuditEvent,
    Match,
    Notification,
    Opportunity,
    OpportunityEvent,
    Watchlist,
)

runner = CliRunner()
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
FAR_FUTURE = datetime(2099, 1, 1, tzinfo=UTC)


@pytest.fixture
def session(upgraded_engine) -> Session:
    factory = sessionmaker(bind=upgraded_engine, autoflush=False, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def _settings(tmp_path, **overrides) -> Settings:
    values = {
        "database_url": os.environ["DATABASE_URL"],
        "outbox_dir": tmp_path,
        "smtp_host": None,
        "smtp_port": 587,
        "smtp_user": None,
        "smtp_pass": None,
        "alert_email_to": None,
        "alert_on_material_deadline_change": True,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _quiet_existing(session: Session) -> None:
    """Hide matches created by other tests inside this transaction."""
    session.execute(update(Match).values(alerted_at=FAR_FUTURE), execution_options={"synchronize_session": False})
    session.execute(
        update(Match)
        .where(Match.status == "new")
        .values(status="seen"),
        execution_options={"synchronize_session": False},
    )
    session.flush()


def _opportunity(session: Session, **overrides) -> Opportunity:
    values = {
        "source": "sam",
        "source_id": f"alert-{uuid4()}",
        "title": "Ship spare parts",
        "agency_path": "Department of the Navy",
        "psc_code": "R425",
        "naics_code": "541512",
        "set_aside_code": "SBA",
        "response_deadline": datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        "estimated_value_min": Decimal(25000),
        "estimated_value_max": Decimal(25000),
        "links": {"ui": "https://sam.gov/opp/alert-1/view"},
        "raw": {"fixture": True},
    }
    values.update(overrides)
    row = Opportunity(**values)
    session.add(row)
    session.flush()
    return row


def _watchlist(session: Session, *, name: str | None = None, enabled: bool = True) -> Watchlist:
    row = create_watchlist(
        session,
        name=name or f"alert-watchlist-{uuid4()}",
        psc_codes=["R4"],
        sources=["sam"],
    )
    row.enabled = enabled
    session.flush()
    return row


def _match(session: Session, opportunity: Opportunity, watchlist: Watchlist, **overrides) -> Match:
    values = {
        "opportunity_id": opportunity.id,
        "watchlist_id": watchlist.id,
        "score": Decimal(1),
        "matched_on": {"score_basis": "rule_hits"},
        "status": "new",
    }
    values.update(overrides)
    row = Match(**values)
    session.add(row)
    session.flush()
    return row


def _html_files(tmp_path) -> list:
    return sorted(tmp_path.glob("digest-*.html"))


def test_calendar_days_and_estimated_value_and_source_link() -> None:
    assert calendar_days_remaining(datetime(2026, 10, 1, 12, tzinfo=UTC), NOW) == 5
    assert calendar_days_remaining(None, NOW) is None
    assert format_estimated_value(None, None) is None
    assert format_estimated_value(Decimal(10), Decimal(20)) == "10–20"
    assert format_estimated_value(Decimal("25000.00"), None) == "25000"
    assert source_link({"ui": "javascript:alert(1)", "self": [{"href": "https://sam.gov/opp/9/view"}]}) == (
        "https://sam.gov/opp/9/view"
    )
    assert source_link({"ui": "javascript:alert(1)"}) is None
    assert source_link(None) is None


def test_empty_day_sends_nothing(session: Session, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _quiet_existing(session)

    def boom(*_args, **_kwargs):
        raise AssertionError("empty day must not deliver")

    monkeypatch.setattr("govcon.alerts.digest.write_outbox", boom)
    monkeypatch.setattr("govcon.alerts.digest.send_smtp", boom)
    before_notifications = session.scalar(select(func.count()).select_from(Notification)) or 0
    result = run_digest(session, settings=_settings(tmp_path), now=NOW)
    assert result.sent is False
    assert result.channel == "none"
    assert result.path is None
    assert result.new_count == 0
    assert result.amendment_count == 0
    assert _html_files(tmp_path) == []
    after_notifications = session.scalar(select(func.count()).select_from(Notification)) or 0
    assert after_notifications == before_notifications


def test_digest_includes_required_fields_and_groups_watchlists(session: Session, tmp_path) -> None:
    _quiet_existing(session)
    first = _watchlist(session, name="alert-watchlist-alpha")
    second = _watchlist(session, name="alert-watchlist-beta")
    known = _opportunity(session, title="Known value hull")
    unknown = _opportunity(
        session,
        title="<script>alert(1)</script>",
        estimated_value_min=None,
        estimated_value_max=None,
        links={"ui": "javascript:alert(1)"},
        agency_path=None,
    )
    _match(session, known, first)
    _match(session, unknown, second)
    result = run_digest(session, settings=_settings(tmp_path), now=NOW)
    assert result.sent is True
    assert result.channel == "outbox"
    assert result.new_count == 2
    html = _html_files(tmp_path)[0].read_text(encoding="utf-8")
    assert html.index("alert-watchlist-alpha") < html.index("alert-watchlist-beta")
    assert "Known value hull" in html
    assert "Department of the Navy" in html
    assert "sam" in html
    assert "R425" in html
    assert "541512" in html
    assert "SBA" in html
    assert "2026-10-01 12:00 UTC" in html
    assert "5 days remaining" in html
    assert "25000" in html
    assert 'href="https://sam.gov/opp/alert-1/view"' in html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "javascript:" not in html
    unknown_at = html.index("&lt;script&gt;")
    assert "not stated" in html[unknown_at:]
    assert "Historical awards" not in html
    assert "Bid recommendation" not in html
    for match in session.scalars(select(Match).where(Match.watchlist_id.in_([first.id, second.id]))):
        assert match.status == "new"  # alerting is not triage; the match stays in the inbox
        assert match.alerted_at == NOW
    audit = session.scalar(
        select(AuditEvent).where(AuditEvent.action_type == "alert_digest_sent").order_by(AuditEvent.id.desc())
    )
    assert audit is not None
    assert audit.new_value["channel"] == "outbox"
    assert set(audit.new_value["match_ids"]) == set(result.match_ids)


def test_repeat_run_does_not_duplicate(session: Session, tmp_path) -> None:
    _quiet_existing(session)
    watchlist = _watchlist(session)
    opportunity = _opportunity(session)
    match = _match(session, opportunity, watchlist)
    first = run_digest(session, settings=_settings(tmp_path), now=NOW)
    assert first.sent is True
    assert len(_html_files(tmp_path)) == 1
    second = run_digest(session, settings=_settings(tmp_path), now=NOW + timedelta(hours=3))
    assert second.sent is False
    assert second.channel == "none"
    assert len(_html_files(tmp_path)) == 1
    assert match.alerted_at == NOW
    assert match.status == "new"


def test_seen_match_is_not_a_new_alert(session: Session, tmp_path) -> None:
    _quiet_existing(session)
    watchlist = _watchlist(session)
    opportunity = _opportunity(session)
    match = _match(session, opportunity, watchlist, status="seen")
    result = run_digest(session, settings=_settings(tmp_path), now=NOW)
    assert result.sent is False
    assert match.alerted_at is None
    assert _html_files(tmp_path) == []


def test_disabled_watchlist_is_omitted(session: Session, tmp_path) -> None:
    _quiet_existing(session)
    watchlist = _watchlist(session, enabled=False)
    opportunity = _opportunity(session)
    match = _match(session, opportunity, watchlist)
    result = run_digest(session, settings=_settings(tmp_path), now=NOW)
    assert result.sent is False
    assert match.alerted_at is None
    assert match.status == "new"


def test_material_deadline_change_can_realert(session: Session, tmp_path) -> None:
    _quiet_existing(session)
    watchlist = _watchlist(session)
    opportunity = _opportunity(session)
    match = _match(session, opportunity, watchlist)
    first = run_digest(session, settings=_settings(tmp_path), now=NOW)
    assert first.new_count == 1
    opportunity.response_deadline = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    session.add(
        OpportunityEvent(
            opportunity_id=opportunity.id,
            event_type="deadline_changed",
            field_name="response_deadline",
            old_value={"value": "2026-10-01T12:00:00+00:00"},
            new_value={"value": "2026-10-06T12:00:00+00:00"},
            detected_at=NOW + timedelta(hours=1),
        )
    )
    session.flush()
    later = NOW + timedelta(hours=2)
    second = run_digest(session, settings=_settings(tmp_path), now=later)
    assert second.sent is True
    assert second.new_count == 0
    assert second.amendment_count == 1
    html = _html_files(tmp_path)[-1].read_text(encoding="utf-8")
    assert "Amendment alerts" in html
    assert "material deadline change" in html
    assert "2026-10-01T12:00:00+00:00" in html
    assert "2026-10-06 12:00 UTC" in html
    assert "10 days remaining" in html
    assert match.alerted_at == later
    assert match.status == "new"
    third = run_digest(session, settings=_settings(tmp_path), now=later + timedelta(hours=1))
    assert third.sent is False
    assert len(_html_files(tmp_path)) == 2


def test_deadline_re_alert_is_optional(session: Session, tmp_path) -> None:
    _quiet_existing(session)
    watchlist = _watchlist(session)
    opportunity = _opportunity(session)
    match = _match(session, opportunity, watchlist)
    run_digest(session, settings=_settings(tmp_path), now=NOW)
    session.add(
        OpportunityEvent(
            opportunity_id=opportunity.id,
            event_type="deadline_changed",
            field_name="response_deadline",
            old_value={"value": "2026-10-01T12:00:00+00:00"},
            new_value={"value": "2026-10-08T12:00:00+00:00"},
            detected_at=NOW + timedelta(minutes=5),
        )
    )
    session.flush()
    result = run_digest(
        session,
        settings=_settings(tmp_path, alert_on_material_deadline_change=False),
        now=NOW + timedelta(hours=1),
    )
    assert result.sent is False
    assert match.alerted_at == NOW
    assert len(_html_files(tmp_path)) == 1


def test_non_deadline_change_does_not_realert(session: Session, tmp_path) -> None:
    _quiet_existing(session)
    watchlist = _watchlist(session)
    opportunity = _opportunity(session)
    _match(session, opportunity, watchlist)
    run_digest(session, settings=_settings(tmp_path), now=NOW)
    session.add(
        OpportunityEvent(
            opportunity_id=opportunity.id,
            event_type="title_changed",
            field_name="title",
            old_value={"value": "Ship spare parts"},
            new_value={"value": "Ship spare parts revised"},
            detected_at=NOW + timedelta(minutes=5),
        )
    )
    session.flush()
    result = run_digest(session, settings=_settings(tmp_path), now=NOW + timedelta(hours=1))
    assert result.sent is False
    assert len(_html_files(tmp_path)) == 1


def test_dismissed_match_gets_no_amendment_alert(session: Session, tmp_path) -> None:
    _quiet_existing(session)
    watchlist = _watchlist(session)
    opportunity = _opportunity(session)
    match = _match(session, opportunity, watchlist)
    run_digest(session, settings=_settings(tmp_path), now=NOW)
    match.status = "dismissed"
    session.add(
        OpportunityEvent(
            opportunity_id=opportunity.id,
            event_type="deadline_changed",
            field_name="response_deadline",
            old_value={"value": "2026-10-01T12:00:00+00:00"},
            new_value={"value": "2026-10-03T12:00:00+00:00"},
            detected_at=NOW + timedelta(minutes=10),
        )
    )
    session.flush()
    result = run_digest(session, settings=_settings(tmp_path), now=NOW + timedelta(hours=1))
    assert result.sent is False and result.amendment_count == 0
    assert match.status == "dismissed"


def _event(session: Session, opportunity: Opportunity, event_type: str, *, minutes: int = 10, **fields) -> None:
    session.add(
        OpportunityEvent(
            opportunity_id=opportunity.id,
            event_type=event_type,
            field_name=fields.get("field_name"),
            old_value=fields.get("old_value"),
            new_value=fields.get("new_value"),
            detected_at=NOW + timedelta(minutes=minutes),
        )
    )
    session.flush()


def test_new_match_alerts_skip_closed_and_expired_opportunities(session: Session, tmp_path) -> None:
    _quiet_existing(session)
    watchlist = _watchlist(session)
    cancelled = _opportunity(session, status="cancelled")
    expired = _opportunity(session, response_deadline=NOW - timedelta(hours=1))
    inactive = _opportunity(session)
    live = _opportunity(session, title="Still open")
    for opportunity in (cancelled, expired, inactive, live):
        _match(session, opportunity, watchlist)
    inactive_match = session.scalar(select(Match).where(Match.opportunity_id == inactive.id))
    inactive_match.active = False
    inactive_match.inactive_reason = "no_longer_matches"
    session.flush()
    result = run_digest(session, settings=_settings(tmp_path), now=NOW)
    assert result.new_count == 1
    live_match = session.scalar(select(Match).where(Match.opportunity_id == live.id))
    assert result.match_ids == [live_match.id]


def test_cancellation_files_and_set_aside_changes_realert(session: Session, tmp_path) -> None:
    _quiet_existing(session)
    watchlist = _watchlist(session)
    files = _opportunity(session, title="Files posted")
    set_aside = _opportunity(session, title="Set-aside changed")
    cancelled = _opportunity(session, title="Cancelled notice")
    matches = {opp.id: _match(session, opp, watchlist) for opp in (files, set_aside, cancelled)}
    run_digest(session, settings=_settings(tmp_path), now=NOW)

    _event(session, files, "files_added", field_name="links", new_value={"value": ["https://sam.gov/f.pdf"]})
    _event(session, set_aside, "set_aside_changed", field_name="set_aside_code",
           old_value={"value": "SBA"}, new_value={"value": "8A"})
    cancelled.status = "cancelled"
    _event(session, cancelled, "cancelled", field_name="status", new_value={"value": "cancelled"})
    # Matching after the cancellation closes the match; the cancellation still alerts.
    cancelled_match = matches[cancelled.id]
    cancelled_match.active = False
    cancelled_match.inactive_reason = "opportunity_closed"
    session.flush()

    result = run_digest(session, settings=_settings(tmp_path), now=NOW + timedelta(hours=1))
    assert result.amendment_count == 3
    html = _html_files(tmp_path)[-1].read_text(encoding="utf-8")
    for label in ("new files posted", "set-aside changed", "opportunity cancelled"):
        assert label in html
    assert "Previous deadline" not in html  # only shown for deadline changes

    again = run_digest(session, settings=_settings(tmp_path), now=NOW + timedelta(hours=2))
    assert again.sent is False


def test_other_changes_to_a_closed_opportunity_do_not_alert(session: Session, tmp_path) -> None:
    _quiet_existing(session)
    watchlist = _watchlist(session)
    opportunity = _opportunity(session)
    _match(session, opportunity, watchlist)
    run_digest(session, settings=_settings(tmp_path), now=NOW)
    opportunity.status = "archived"
    _event(session, opportunity, "files_added", field_name="links", new_value={"value": ["https://sam.gov/x.pdf"]})
    result = run_digest(session, settings=_settings(tmp_path), now=NOW + timedelta(hours=1))
    assert result.sent is False


def test_files_alert_even_when_deadline_realerts_are_off(session: Session, tmp_path) -> None:
    _quiet_existing(session)
    watchlist = _watchlist(session)
    opportunity = _opportunity(session)
    _match(session, opportunity, watchlist)
    run_digest(session, settings=_settings(tmp_path), now=NOW)
    _event(session, opportunity, "deadline_changed", minutes=5, field_name="response_deadline",
           old_value={"value": "2026-10-01T12:00:00+00:00"}, new_value={"value": "2026-10-02T12:00:00+00:00"})
    _event(session, opportunity, "files_added", minutes=6, field_name="links", new_value={"value": ["https://sam.gov/y.pdf"]})
    settings = _settings(tmp_path, alert_on_material_deadline_change=False)
    result = run_digest(session, settings=settings, now=NOW + timedelta(hours=1))
    assert result.amendment_count == 1
    html = _html_files(tmp_path)[-1].read_text(encoding="utf-8")
    assert "new files posted" in html and "material deadline change" not in html


def test_matching_pipeline_then_digest(session: Session, tmp_path) -> None:
    _quiet_existing(session)
    watchlist = _watchlist(session)
    watchlist.psc_codes = ["Z9"]
    session.flush()
    opportunity = _opportunity(session, title="Matched through the engine", psc_code="Z999")
    stats = run_matching(session, watchlist_id=watchlist.id, now=NOW)
    assert stats.inserted == 1
    created = session.scalar(
        select(Match).where(Match.watchlist_id == watchlist.id, Match.opportunity_id == opportunity.id)
    )
    assert created is not None
    result = run_digest(session, settings=_settings(tmp_path), now=NOW)
    assert result.new_count == 1
    html = _html_files(tmp_path)[0].read_text(encoding="utf-8")
    assert "Matched through the engine" in html
    again = run_digest(session, settings=_settings(tmp_path), now=NOW + timedelta(minutes=1))
    assert again.sent is False


class _FakeSMTP:
    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.messages = []
        self.logged_in = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def ehlo(self):
        return None

    def has_extn(self, name):
        return name == "starttls"

    def starttls(self, context=None):
        self.tls_context = context

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, message):
        self.messages.append(message)


def test_configured_smtp_still_writes_the_outbox_by_default(session: Session, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured SMTP server must not be contacted unless a caller opts in."""
    _quiet_existing(session)

    class BoomSMTP:
        def __init__(self, *args, **kwargs):
            raise AssertionError("SMTP must not be opened")

    monkeypatch.setattr("govcon.alerts.digest.smtplib.SMTP", BoomSMTP)
    watchlist = _watchlist(session)
    _match(session, _opportunity(session, title="Outbox only hull"), watchlist)
    settings = _settings(
        tmp_path,
        smtp_host="smtp.example.test",
        smtp_user="alerts@example.test",
        smtp_pass="supersecret-value",
        alert_email_to="ops@example.test",
    )
    result = run_digest(session, settings=settings, now=NOW)
    assert result.channel == "outbox"
    assert result.path is not None
    assert "Outbox only hull" in Path(result.path).read_text(encoding="utf-8")


def test_smtp_sends_html_and_skips_outbox(session: Session, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _quiet_existing(session)
    created: list[_FakeSMTP] = []

    class RecordingSMTP(_FakeSMTP):
        def __init__(self, host, port, timeout=None):
            super().__init__(host, port, timeout)
            created.append(self)

    monkeypatch.setattr("govcon.alerts.digest.smtplib.SMTP", RecordingSMTP)
    watchlist = _watchlist(session)
    _match(session, _opportunity(session, title="SMTP hull"), watchlist)
    settings = _settings(
        tmp_path,
        smtp_host="smtp.example.test",
        smtp_user="alerts@example.test",
        smtp_pass="supersecret-value",
        alert_email_to="ops@example.test",
    )
    result = run_digest(session, settings=settings, now=NOW, external_delivery=True)
    assert result.sent is True
    assert result.channel == "smtp"
    assert result.path is None
    assert _html_files(tmp_path) == []
    assert len(created) == 1
    message = created[0].messages[0]
    assert message["To"] == "ops@example.test"
    assert message["From"] == "alerts@example.test"
    html = next(part.get_content() for part in message.walk() if part.get_content_type() == "text/html")
    assert "SMTP hull" in html
    assert created[0].logged_in == ("alerts@example.test", "supersecret-value")
    # STARTTLS used a certificate- and host-verifying context before login.
    assert created[0].tls_context.check_hostname and created[0].tls_context.verify_mode == ssl.CERT_REQUIRED


def test_smtp_failure_leaves_match_unalerted(session: Session, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _quiet_existing(session)

    class BoomSMTP(_FakeSMTP):
        def send_message(self, message):
            raise smtplib.SMTPException("relay denied for supersecret-value")

    monkeypatch.setattr("govcon.alerts.digest.smtplib.SMTP", BoomSMTP)
    watchlist = _watchlist(session)
    match = _match(session, _opportunity(session), watchlist)
    settings = _settings(
        tmp_path,
        smtp_host="smtp.example.test",
        smtp_user="alerts@example.test",
        smtp_pass="supersecret-value",
        alert_email_to="ops@example.test",
    )
    with pytest.raises(DigestDeliveryError) as caught:
        run_digest(session, settings=settings, now=NOW, external_delivery=True)
    assert "supersecret-value" not in str(caught.value)
    assert match.alerted_at is None
    assert match.status == "new"
    assert _html_files(tmp_path) == []


def test_smtp_error_redacts_password(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    class AuthBoom:
        def __init__(self, host, port, timeout=None):
            self.host = host

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def ehlo(self):
            return None

        def has_extn(self, _name):
            return False

        def login(self, _user, password):
            raise smtplib.SMTPAuthenticationError(535, f"bad {password}")

        def send_message(self, _message):
            raise AssertionError("send should not run")

    monkeypatch.setattr("govcon.alerts.digest.smtplib.SMTP", AuthBoom)
    settings = _settings(
        tmp_path,
        smtp_host="smtp.example.test",
        smtp_user="alerts@example.test",
        smtp_pass="supersecret-value",
        alert_email_to="ops@example.test",
    )
    with pytest.raises(DigestDeliveryError) as caught:
        send_smtp(settings, subject="x", html="<p>x</p>", plain="x")
    assert "supersecret-value" not in str(caught.value)


def test_alerts_help_lists_digest() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "alerts" in result.stdout
    digest_help = runner.invoke(app, ["alerts", "--help"])
    assert digest_help.exit_code == 0
    assert "digest" in digest_help.stdout


def test_alerts_digest_cli_outbox_and_repeat(session: Session, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    sealed = session.execute(
        select(Match.id, Match.status, Match.alerted_at).where(Match.status == "new", Match.alerted_at.is_(None))
    ).all()
    if sealed:
        session.execute(
            update(Match)
            .where(Match.id.in_([row.id for row in sealed]))
            .values(status="seen", alerted_at=FAR_FUTURE),
            execution_options={"synchronize_session": False},
        )
        session.commit()
    watchlist_id = None
    opportunity_id = None
    try:
        monkeypatch.setenv("OUTBOX_DIR", str(tmp_path))
        monkeypatch.setenv("SMTP_HOST", "")
        monkeypatch.setenv("ALERT_EMAIL_TO", "")
        monkeypatch.setenv("ALERT_ON_MATERIAL_DEADLINE_CHANGE", "true")
        from govcon.config import get_settings

        get_settings.cache_clear()
        watchlist = _watchlist(session, name=f"alert-watchlist-{uuid4()}")
        # The CLI runs on the wall clock, so the deadline must be in the future.
        opportunity = _opportunity(
            session, title="CLI digest hull", response_deadline=datetime.now(UTC) + timedelta(days=10)
        )
        _match(session, opportunity, watchlist)
        watchlist_id = watchlist.id
        opportunity_id = opportunity.id
        session.commit()

        first = runner.invoke(app, ["alerts", "digest"])
        assert first.exit_code == 0, first.output
        assert "message: yes" in first.stdout
        assert "channel: outbox" in first.stdout
        assert "new_matches: 1" in first.stdout
        assert len(_html_files(tmp_path)) == 1
        session.expire_all()
        stored = session.get(Match, session.scalar(select(Match.id).where(Match.watchlist_id == watchlist_id)))
        assert stored is not None
        assert stored.status == "new"
        assert stored.alerted_at is not None

        second = runner.invoke(app, ["alerts", "digest"])
        assert second.exit_code == 0, second.output
        assert "message: no" in second.stdout
        assert len(_html_files(tmp_path)) == 1
    finally:
        if opportunity_id is not None:
            session.execute(delete(OpportunityEvent).where(OpportunityEvent.opportunity_id == opportunity_id))
            session.execute(delete(Match).where(Match.opportunity_id == opportunity_id))
            session.execute(delete(Opportunity).where(Opportunity.id == opportunity_id))
        if watchlist_id is not None:
            session.execute(delete(Match).where(Match.watchlist_id == watchlist_id))
            session.execute(delete(Watchlist).where(Watchlist.id == watchlist_id))
        session.execute(delete(AuditEvent).where(AuditEvent.action_type == "alert_digest_sent"))
        for row in sealed:
            session.execute(
                update(Match).where(Match.id == row.id).values(status=row.status, alerted_at=row.alerted_at),
                execution_options={"synchronize_session": False},
            )
        session.commit()


def test_digest_cli_requires_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "")
    from govcon.config import get_settings

    get_settings.cache_clear()
    result = runner.invoke(app, ["alerts", "digest"])
    assert result.exit_code == 2
    assert "DATABASE_URL" in result.output
