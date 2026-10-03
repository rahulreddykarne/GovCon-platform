"""Phase 1 SAM.gov ingestion, snapshots, and archive sweep.

The search fixture is the response example published by GSA on
https://open.gsa.gov/api/get-opportunities-public-api/ (notice
5b345bbb7127b91a3ad577b203fc6f68). Normal tests do not call the network.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session, sessionmaker
from tenacity import wait_exponential
from typer.testing import CliRunner

from govcon.cli import app
from govcon.ingest.sam_opportunities import (
    SAM_SEARCH_URL,
    SamApiError,
    archive_expired_sam_opportunities,
    default_posted_window,
    ingest_opportunity_records,
    iter_backfill_windows,
    iter_search_records,
    parse_estimated_values,
    parse_nsn_candidates,
    parse_quantity,
    pull_sam_opportunities,
)
from govcon.models import (
    Contact,
    IngestionRun,
    Opportunity,
    OpportunityEvent,
    OpportunitySnapshot,
)

runner = CliRunner()
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sam_opportunities_search.json"
PUBLISHED_NOTICE_ID = "5b345bbb7127b91a3ad577b203fc6f68"
FAST_WAIT = wait_exponential(multiplier=0.01, min=0.01, max=0.02)


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _record() -> dict:
    return deepcopy(_load_fixture()["opportunitiesData"][0])


@pytest.fixture
def session(upgraded_engine) -> Session:
    factory = sessionmaker(bind=upgraded_engine, autoflush=False, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def _purge(db: Session, source_ids: set[str], emails: set[str] | None = None) -> None:
    """Remove all rows associated with the given SAM source_ids, respecting FK order.

    Uses raw SQL to delete in reverse-dependency order so this remains correct
    even after phases 7–19 add compliance, AI-analysis, proposal, and submission
    rows for the same fixture opportunity.
    """
    db.rollback()
    ids_list = list(source_ids)
    opp_sub = "SELECT id FROM opportunities WHERE source = 'sam' AND source_id = ANY(:ids)"
    p = {"ids": ids_list}
    # Delete in topological order (leaves first, then their parents).
    # proposal_sections → proposal_versions (circular with proposals.current_version_id)
    db.execute(text(f"DELETE FROM proposal_sections WHERE proposal_version_id IN (SELECT id FROM proposal_versions WHERE proposal_id IN (SELECT id FROM proposals WHERE opportunity_id IN ({opp_sub})))"), p)
    db.execute(text(f"UPDATE proposals SET current_version_id = NULL, approved_version_id = NULL WHERE opportunity_id IN ({opp_sub})"), p)
    db.execute(text(f"DELETE FROM proposal_versions WHERE proposal_id IN (SELECT id FROM proposals WHERE opportunity_id IN ({opp_sub}))"), p)
    db.execute(text(f"DELETE FROM proposals WHERE opportunity_id IN ({opp_sub})"), p)
    db.execute(text(f"DELETE FROM submissions WHERE opportunity_id IN ({opp_sub})"), p)
    db.execute(text(f"DELETE FROM outcome_feedback WHERE opportunity_id IN ({opp_sub})"), p)
    db.execute(text(f"DELETE FROM review_notes WHERE opportunity_id IN ({opp_sub})"), p)
    db.execute(text(f"DELETE FROM review_comments WHERE opportunity_id IN ({opp_sub})"), p)
    db.execute(text(f"DELETE FROM review_assignments WHERE opportunity_id IN ({opp_sub})"), p)
    db.execute(text(f"DELETE FROM review_sessions WHERE opportunity_id IN ({opp_sub})"), p)
    db.execute(text(f"DELETE FROM bid_decisions WHERE opportunity_id IN ({opp_sub})"), p)
    db.execute(text(f"DELETE FROM decision_runs WHERE opportunity_id IN ({opp_sub})"), p)
    db.execute(text(f"DELETE FROM pursuits WHERE opportunity_id IN ({opp_sub})"), p)
    db.execute(text(f"DELETE FROM compliance_findings WHERE opportunity_id IN ({opp_sub})"), p)
    db.execute(text(f"DELETE FROM requirement_evidence WHERE requirement_id IN (SELECT id FROM requirements WHERE opportunity_id IN ({opp_sub}))"), p)
    # requirements.compliance_run_id → compliance_runs: delete requirements first
    db.execute(text(f"DELETE FROM requirements WHERE opportunity_id IN ({opp_sub})"), p)
    db.execute(text(f"DELETE FROM compliance_runs WHERE opportunity_id IN ({opp_sub})"), p)
    db.execute(text(f"DELETE FROM ai_analyses WHERE opportunity_id IN ({opp_sub})"), p)
    db.execute(text(f"DELETE FROM files WHERE opportunity_id IN ({opp_sub})"), p)
    db.execute(text(f"DELETE FROM notifications WHERE opportunity_id IN ({opp_sub})"), p)
    db.execute(text(f"DELETE FROM audit_events WHERE opportunity_id IN ({opp_sub})"), p)
    db.execute(text(f"DELETE FROM opportunity_events WHERE opportunity_id IN ({opp_sub})"), p)
    db.execute(text(f"DELETE FROM opportunity_snapshots WHERE opportunity_id IN ({opp_sub})"), p)
    db.execute(text(f"DELETE FROM matches WHERE opportunity_id IN ({opp_sub})"), p)
    db.execute(text(f"DELETE FROM contacts WHERE first_seen_opportunity_id IN ({opp_sub})"), p)
    if emails:
        db.execute(delete(Contact).where(Contact.email.in_(emails)))
    db.execute(text("DELETE FROM opportunities WHERE source = 'sam' AND source_id = ANY(:ids)"), {"ids": ids_list})
    db.commit()


def _counts(db: Session, source_id: str) -> tuple[int, int, int]:
    opportunity_id = db.scalar(
        select(Opportunity.id).where(Opportunity.source == "sam", Opportunity.source_id == source_id)
    )
    assert opportunity_id is not None
    snapshots = db.scalar(
        select(func.count()).select_from(OpportunitySnapshot).where(OpportunitySnapshot.opportunity_id == opportunity_id)
    )
    events = db.scalar(
        select(func.count()).select_from(OpportunityEvent).where(OpportunityEvent.opportunity_id == opportunity_id)
    )
    return int(snapshots), int(events), int(opportunity_id)


def test_fixture_is_the_published_sam_example() -> None:
    payload = _load_fixture()
    record = payload["opportunitiesData"][0]
    assert payload["totalRecords"] == 34
    assert record["noticeId"] == PUBLISHED_NOTICE_ID
    assert record["award"]["amount"] == "800620"
    assert record["description"].startswith("https://api.sam.gov/")
    assert "api_key=" not in FIXTURE_PATH.read_text(encoding="utf-8")


def test_default_window_is_last_three_days() -> None:
    start, end = default_posted_window(date(2026, 9, 26))
    assert (start, end) == (date(2026, 9, 24), date(2026, 9, 26))


def test_backfill_windows_stay_within_one_year() -> None:
    windows = iter_backfill_windows(date(2020, 1, 1), date(2022, 6, 15))
    assert windows == [
        (date(2020, 1, 1), date(2021, 1, 1)),
        (date(2021, 1, 2), date(2022, 1, 2)),
        (date(2022, 1, 3), date(2022, 6, 15)),
    ]
    for start, end in windows:
        assert end <= date(start.year + 1, start.month, start.day)


def test_nsn_quantity_and_estimated_value_are_explicit_only() -> None:
    title = "Bolt NSN 5305-00-123-4567 quantity 12 EA"
    description = "Estimated value $1,000 to $2,500. Phone 2174941263."
    assert parse_nsn_candidates(title, description) == ("5305-00-123-4567",)
    assert parse_quantity(title, description) == (Decimal(12), "EA")
    low, high, source = parse_estimated_values({}, title, description)
    assert (low, high, source) == (Decimal(1000), Decimal(2500), "explicit_text")
    bare = parse_quantity("Widget 12 EA for NAICS 236220", None)
    assert bare == (None, None)
    award_only = {"award": {"amount": "800620"}}
    assert parse_estimated_values(award_only, "Historic Office Renovation", None) == (None, None, None)
    labeled = parse_nsn_candidates(None, "National Stock Number 5310 00 456 7890")
    assert labeled == ("5310-00-456-7890",)


def test_rerun_unchanged_fixture_inserts_nothing_new(session: Session) -> None:
    _purge(session, {PUBLISHED_NOTICE_ID}, {"jesse.jones@gsa.gov"})
    record = _record()
    try:
        first = ingest_opportunity_records(session, [record])
        session.commit()
        second = ingest_opportunity_records(session, [deepcopy(record)])
        session.commit()
        snapshots, events, _opportunity_id = _counts(session, PUBLISHED_NOTICE_ID)
        opportunities = session.scalar(
            select(func.count())
            .select_from(Opportunity)
            .where(Opportunity.source == "sam", Opportunity.source_id == PUBLISHED_NOTICE_ID)
        )
        contacts = session.scalar(select(func.count()).select_from(Contact).where(Contact.email == "jesse.jones@gsa.gov"))
        assert first.inserted == 1
        assert second.inserted == 0
        assert second.updated == 0
        assert second.unchanged == 1
        assert opportunities == 1
        assert snapshots == 1
        assert events == 1
        assert contacts == 1
    finally:
        _purge(session, {PUBLISHED_NOTICE_ID}, {"jesse.jones@gsa.gov"})


def test_changed_payload_creates_one_snapshot_and_deadline_event(session: Session) -> None:
    _purge(session, {PUBLISHED_NOTICE_ID}, {"jesse.jones@gsa.gov"})
    record = _record()
    changed = deepcopy(record)
    changed["responseDeadLine"] = "2026-11-15T17:00:00-05:00"
    try:
        ingest_opportunity_records(session, [record])
        session.commit()
        updated = ingest_opportunity_records(session, [changed])
        session.commit()
        again = ingest_opportunity_records(session, [deepcopy(changed)])
        session.commit()
        snapshots, _events, opportunity_id = _counts(session, PUBLISHED_NOTICE_ID)
        deadline_events = session.scalars(
            select(OpportunityEvent).where(
                OpportunityEvent.opportunity_id == opportunity_id,
                OpportunityEvent.event_type == "deadline_changed",
            )
        ).all()
        opportunity = session.get(Opportunity, opportunity_id)
        assert updated.updated == 1
        assert updated.inserted == 0
        assert snapshots == 2
        assert again.unchanged == 1
        assert len(deadline_events) == 1
        event = deadline_events[0]
        assert event.field_name == "response_deadline"
        assert event.old_value == {"value": None}
        assert event.new_value["value"].startswith("2026-11-15T17:00:00")
        assert event.snapshot_id is not None
        snapshot = session.get(OpportunitySnapshot, event.snapshot_id)
        assert snapshot is not None
        assert snapshot.content_hash == opportunity.raw_hash
        assert snapshot.raw["responseDeadLine"] == "2026-11-15T17:00:00-05:00"
        assert opportunity.response_deadline is not None
    finally:
        _purge(session, {PUBLISHED_NOTICE_ID}, {"jesse.jones@gsa.gov"})


def test_revert_to_an_earlier_version_updates_the_live_row(session: Session) -> None:
    """A -> B -> A: the third pull is an update, not 'unchanged' (M1)."""
    _purge(session, {PUBLISHED_NOTICE_ID}, {"jesse.jones@gsa.gov"})
    version_a = _record()
    version_a["responseDeadLine"] = "2026-11-01T17:00:00-05:00"
    version_a["typeOfSetAside"] = None
    version_b = deepcopy(version_a)
    version_b["responseDeadLine"] = "2026-11-15T17:00:00-05:00"
    version_b["typeOfSetAside"] = "SBA"
    try:
        ingest_opportunity_records(session, [version_a])
        session.commit()
        ingest_opportunity_records(session, [version_b])
        session.commit()
        reverted = ingest_opportunity_records(session, [deepcopy(version_a)])
        session.commit()
        snapshots, _events, opportunity_id = _counts(session, PUBLISHED_NOTICE_ID)
        opportunity = session.get(Opportunity, opportunity_id)
        session.refresh(opportunity)

        assert reverted.updated == 1 and reverted.unchanged == 0
        assert snapshots == 2  # A's snapshot is reused, never duplicated
        from datetime import UTC, datetime

        assert opportunity.response_deadline == datetime(2026, 11, 1, 22, 0, tzinfo=UTC)
        assert opportunity.set_aside_code is None

        snapshot_a = session.scalar(
            select(OpportunitySnapshot).where(
                OpportunitySnapshot.opportunity_id == opportunity_id,
                OpportunitySnapshot.raw["responseDeadLine"].astext == "2026-11-01T17:00:00-05:00",
            )
        )
        assert snapshot_a is not None and snapshot_a.content_hash == opportunity.raw_hash

        deadline_events = session.scalars(
            select(OpportunityEvent)
            .where(OpportunityEvent.opportunity_id == opportunity_id, OpportunityEvent.event_type == "deadline_changed")
            .order_by(OpportunityEvent.id)
        ).all()
        set_aside_events = session.scalars(
            select(OpportunityEvent)
            .where(OpportunityEvent.opportunity_id == opportunity_id, OpportunityEvent.event_type == "set_aside_changed")
            .order_by(OpportunityEvent.id)
        ).all()
        assert len(deadline_events) == 2 and len(set_aside_events) == 2
        revert_event = deadline_events[-1]
        assert revert_event.new_value["value"].startswith("2026-11-01")
        assert revert_event.snapshot_id == snapshot_a.id
        assert set_aside_events[-1].snapshot_id == snapshot_a.id

        from govcon.enrich.attachments import latest_snapshot_id

        assert latest_snapshot_id(session, opportunity_id) == snapshot_a.id

        again = ingest_opportunity_records(session, [deepcopy(version_a)])
        session.commit()
        assert again.unchanged == 1
    finally:
        _purge(session, {PUBLISHED_NOTICE_ID}, {"jesse.jones@gsa.gov"})


def test_raw_source_json_remains_available(session: Session) -> None:
    _purge(session, {PUBLISHED_NOTICE_ID}, {"jesse.jones@gsa.gov"})
    record = _record()
    try:
        ingest_opportunity_records(session, [record])
        session.commit()
        session.expire_all()
        opportunity = session.scalar(
            select(Opportunity).where(Opportunity.source == "sam", Opportunity.source_id == PUBLISHED_NOTICE_ID)
        )
        assert opportunity is not None
        assert opportunity.raw["noticeId"] == record["noticeId"]
        assert opportunity.raw["award"] == record["award"]
        assert opportunity.raw["solicitationNumber"] == " 47PF0018R0023 "
        assert opportunity.raw["pointOfContact"] == record["pointOfContact"]
        assert opportunity.solicitation_number == "47PF0018R0023"
        assert opportunity.estimated_value_min is None
        assert opportunity.estimated_value_max is None
        assert opportunity.naics_code == "236220"
        assert opportunity.psc_code == "Z"
        assert opportunity.status == "open"
        assert opportunity.raw_hash
        snapshot = session.scalar(
            select(OpportunitySnapshot).where(OpportunitySnapshot.opportunity_id == opportunity.id)
        )
        assert snapshot is not None
        assert snapshot.raw["description"] == record["description"]
        assert snapshot.content_hash == opportunity.raw_hash
        assert snapshot.normalized["source_id"] == PUBLISHED_NOTICE_ID
    finally:
        _purge(session, {PUBLISHED_NOTICE_ID}, {"jesse.jones@gsa.gov"})


def test_tracked_field_diffs_reference_the_new_snapshot(session: Session) -> None:
    record = _record()
    record["noticeId"] = "phase1-diff-fields"
    record["resourceLinks"] = ["https://sam.gov/files/original.pdf"]
    changed = deepcopy(record)
    changed["title"] = "Bolt NSN 5305-00-123-4567 quantity 4 EA"
    changed["typeOfSetAside"] = "SBA"
    changed["active"] = "No"
    changed["description"] = "Estimated value $10 to $20. NSN 5305-00-123-4567."
    changed["resourceLinks"] = ["https://sam.gov/files/amended.pdf"]
    changed["uiLink"] = "https://sam.gov/opp/phase1-diff-fields/view"
    try:
        ingest_opportunity_records(session, [record])
        ingest_opportunity_records(session, [changed])
        session.commit()
        opportunity = session.scalar(
            select(Opportunity).where(Opportunity.source == "sam", Opportunity.source_id == "phase1-diff-fields")
        )
        assert opportunity is not None
        assert opportunity.nsn == "5305-00-123-4567"
        assert opportunity.quantity == Decimal(4)
        assert opportunity.unit == "ea"
        assert opportunity.estimated_value_min == Decimal(10)
        assert opportunity.estimated_value_max == Decimal(20)
        assert opportunity.estimated_value_source == "explicit_text"
        assert opportunity.set_aside_code == "SBA"
        assert opportunity.status == "archived"
        events = session.scalars(
            select(OpportunityEvent).where(OpportunityEvent.opportunity_id == opportunity.id)
        ).all()
        by_type = {event.event_type: event for event in events}
        assert "created" in by_type
        for event_type in (
            "title_changed",
            "set_aside_changed",
            "status_changed",
            "quantity_changed",
            "description_changed",
            "files_added",
            "files_removed",
            "links_changed",
        ):
            assert event_type in by_type
            assert by_type[event_type].snapshot_id is not None
        assert by_type["files_added"].new_value["value"] == ["https://sam.gov/files/amended.pdf"]
        assert by_type["files_removed"].old_value["value"] == ["https://sam.gov/files/original.pdf"]
        value_events = [event for event in events if event.event_type == "estimated_value_changed"]
        assert {event.field_name for event in value_events} == {"estimated_value_min", "estimated_value_max"}
        snapshot_ids = {event.snapshot_id for event in events if event.event_type != "created"}
        assert len(snapshot_ids) == 1
    finally:
        _purge(session, {"phase1-diff-fields"})


def test_pagination_uses_page_index(session: Session) -> None:
    offsets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        offsets.append(request.url.params["offset"])
        assert request.url.params["limit"] == "1"
        assert request.url.params["postedFrom"] == "09/24/2026"
        assert request.url.params["postedTo"] == "09/26/2026"
        assert "api_key" in request.url.params
        page = int(request.url.params["offset"])
        notice = f"phase1-page-{page}"
        if page >= 2:
            return httpx.Response(200, json={"totalRecords": 2, "opportunitiesData": []})
        return httpx.Response(
            200,
            json={
                "totalRecords": 2,
                "limit": 1,
                "offset": page,
                "opportunitiesData": [{"noticeId": notice, "title": f"Page {page}", "active": "Yes"}],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        stats = pull_sam_opportunities(
            session,
            api_key="test-key",
            posted_from=date(2026, 9, 24),
            posted_to=date(2026, 9, 26),
            limit=1,
            client=client,
            attempts=1,
            wait=FAST_WAIT,
        )
        session.commit()
        assert offsets == ["0", "1"]
        assert stats.fetched == 2
        assert stats.inserted == 2
        stored = session.scalar(
            select(func.count()).select_from(Opportunity).where(Opportunity.source_id.in_(["phase1-page-0", "phase1-page-1"]))
        )
        assert stored == 2
    finally:
        client.close()
        _purge(session, {"phase1-page-0", "phase1-page-1"})


def test_overlapping_page_stops_instead_of_truncating() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "totalRecords": 3,
                "opportunitiesData": [{"noticeId": "phase1-overlap", "title": "Same page", "active": "Yes"}],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(SamApiError, match="page index"):
            list(
                iter_search_records(
                    client,
                    api_key="test-key",
                    posted_from=date(2026, 9, 24),
                    posted_to=date(2026, 9, 26),
                    limit=1,
                    attempts=1,
                    wait=FAST_WAIT,
                )
            )
    finally:
        client.close()


def test_429_then_success_uses_shared_retry(session: Session) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, json={"error": "rate limit"})
        return httpx.Response(
            200,
            json={
                "totalRecords": 1,
                "opportunitiesData": [{"noticeId": "phase1-retry", "title": "Retried", "active": "Yes"}],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        stats = pull_sam_opportunities(
            session,
            api_key="test-key",
            posted_from=date(2026, 9, 24),
            posted_to=date(2026, 9, 26),
            limit=1,
            client=client,
            attempts=4,
            wait=FAST_WAIT,
        )
        assert calls["n"] == 3
        assert stats.inserted == 1
    finally:
        client.close()
        _purge(session, {"phase1-retry"})


def test_empty_404_is_not_a_successful_empty_pull() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(SamApiError, match="404"):
            list(
                iter_search_records(
                    client,
                    api_key="test-key",
                    posted_from=date(2018, 1, 1),
                    posted_to=date(2018, 5, 10),
                    limit=1,
                    attempts=1,
                    wait=FAST_WAIT,
                )
            )
    finally:
        client.close()


def test_archive_sweep_does_not_call_the_network(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("archive sweep called the network")

    monkeypatch.setattr("govcon.ingest.sam_opportunities.build_client", boom)
    monkeypatch.setattr("govcon.ingest.sam_opportunities.request_with_retry", boom)
    expired = _record()
    expired["noticeId"] = "phase1-archive-past"
    expired["archiveDate"] = "2020-01-01"
    expired["active"] = "Yes"
    current = deepcopy(expired)
    current["noticeId"] = "phase1-archive-future"
    current["archiveDate"] = "2099-01-01"
    try:
        ingest_opportunity_records(session, [expired, current])
        session.commit()
        past = session.scalar(select(Opportunity).where(Opportunity.source_id == "phase1-archive-past"))
        future = session.scalar(select(Opportunity).where(Opportunity.source_id == "phase1-archive-future"))
        watched = [past.id, future.id]
        before = session.scalar(
            select(func.count()).select_from(OpportunitySnapshot).where(OpportunitySnapshot.opportunity_id.in_(watched))
        )
        stats = archive_expired_sam_opportunities(session, today=date(2026, 9, 26))
        session.commit()
        session.refresh(past)
        session.refresh(future)
        after = session.scalar(
            select(func.count()).select_from(OpportunitySnapshot).where(OpportunitySnapshot.opportunity_id.in_(watched))
        )
        event = session.scalar(
            select(OpportunityEvent).where(
                OpportunityEvent.opportunity_id == past.id,
                OpportunityEvent.event_type == "status_changed",
            )
        )
        assert stats.updated == 1
        assert past.status == "archived"
        assert future.status == "open"
        assert before == after
        assert event is not None
        assert event.snapshot_id is None
        assert event.old_value == {"value": "open"}
        assert event.new_value == {"value": "archived"}
    finally:
        _purge(session, {"phase1-archive-past", "phase1-archive-future"}, {"jesse.jones@gsa.gov"})


def test_missing_api_key_fails_at_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAM_API_KEY", "")
    result = runner.invoke(app, ["ingest", "sam"])
    assert result.exit_code == 2
    assert "SAM_API_KEY" in result.output
    assert "test-key" not in result.output


def test_sam_command_rejects_ranges_over_one_year(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAM_API_KEY", "test-key")
    result = runner.invoke(
        app,
        ["ingest", "sam", "--posted-from", "01/01/2020", "--posted-to", "01/02/2022"],
    )
    assert result.exit_code == 2
    assert "sam-backfill" in result.output


def test_ingest_help_lists_sam_commands() -> None:
    result = runner.invoke(app, ["ingest", "--help"])
    assert result.exit_code == 0
    assert "sam" in result.stdout
    assert "sam-backfill" in result.stdout
    assert "sam-archive-sweep" in result.stdout
    assert SAM_SEARCH_URL.startswith("https://api.sam.gov/opportunities/v2/search")


def test_archive_sweep_command_records_a_run(upgraded_engine) -> None:
    result = runner.invoke(app, ["ingest", "sam-archive-sweep"])
    assert result.exit_code == 0
    assert "status: succeeded" in result.output
    with sessionmaker(bind=upgraded_engine)() as db:
        run = db.scalar(select(IngestionRun).where(IngestionRun.job == "sam_archive_sweep").order_by(IngestionRun.id.desc()))
        assert run is not None
        assert run.status == "succeeded"
        assert run.finished_at is not None


@pytest.mark.skipif(not os.environ.get("SAM_API_KEY"), reason="SAM_API_KEY is not set")
def test_live_pull_yields_at_least_one_record(session: Session) -> None:
    posted_from, posted_to = default_posted_window()
    stats = pull_sam_opportunities(
        session,
        api_key=os.environ["SAM_API_KEY"],
        posted_from=posted_from,
        posted_to=posted_to,
        limit=1,
    )
    session.commit()
    assert stats.fetched >= 1
    stored = session.scalar(select(func.count()).select_from(Opportunity).where(Opportunity.source == "sam"))
    assert stored >= 1
