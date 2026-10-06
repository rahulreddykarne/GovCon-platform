"""Phase 5 USAspending awards and pricing tests."""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, inspect, select, update
from sqlalchemy.orm import Session, sessionmaker
from tenacity import wait_exponential
from typer.testing import CliRunner

from govcon.alerts.digest import run_digest
from govcon.cli import app
from govcon.config import Settings
from govcon.ingest.runs import IngestStats, finish_run, start_run
from govcon.ingest.usaspending import (
    JOB_NAME,
    UsaSpendingError,
    derive_quantity_and_unit_price,
    ingest_award_records,
    load_search_document,
    normalize_award,
    plan_details,
    plan_pull,
    pull_usaspending,
)
from govcon.intelligence.awards import (
    AwardeeTotal,
    award_history_for_agency,
    recompete_candidates,
    top_awardees,
)
from govcon.matching.pricing import price_history, price_history_psc
from govcon.matching.watchlists import create_watchlist
from govcon.models import Award, IngestionRun, Match, Opportunity, Watchlist
from govcon.paths import repo_root

runner = CliRunner()
FAST_WAIT = wait_exponential(multiplier=0.01, min=0.01, max=0.05)
FIXTURE = repo_root() / "tests" / "fixtures" / "usaspending_spending_by_award.json"
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
FIXTURE_IDS = (
    "CONT_AWD_SPE2DM26FVSMW_9700_SPE2DM20D5004_9700",
    "CONT_AWD_SPE2DM26FVSMV_9700_SPE2DM20D5004_9700",
)


@pytest.fixture
def session(upgraded_engine) -> Session:
    factory = sessionmaker(bind=upgraded_engine, autoflush=False, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def _settings(tmp_path) -> Settings:
    return Settings(
        database_url=os.environ["DATABASE_URL"],
        outbox_dir=tmp_path,
        smtp_host=None,
        alert_email_to=None,
    )


def _isolate_watchlists(session: Session) -> None:
    session.execute(update(Watchlist).values(enabled=False))
    session.flush()


def _award(session: Session, **overrides) -> Award:
    award_id = overrides.get("award_id", f"phase5-{uuid4().hex}")
    values = {
        "source": "usaspending",
        "award_id": award_id,
        "piid": "PIID-1",
        "description": "sample award",
        "psc_code": "6515",
        "naics_code": "423450",
        "nsn": "6545-01-632-0167",
        "recipient_uei": "UEI-ONE",
        "recipient_name": "Known Vendor",
        "awarding_agency": "Department of Defense / Defense Logistics Agency",
        "action_date": date(2024, 6, 1),
        "total_obligation": Decimal(1000),
        "quantity": None,
        "unit_price": None,
        "raw": {"generated_internal_id": award_id},
    }
    values.update(overrides)
    row = Award(**values)
    session.add(row)
    session.flush()
    return row


def _search_row(**overrides) -> dict:
    row = {
        "generated_internal_id": f"CONT_AWD_PHASE5_{uuid4().hex}",
        "Award ID": "PHASE5PIID",
        "Recipient Name": "Search Vendor",
        "Recipient UEI": "SEARCHUEI0001",
        "Awarding Agency": "Department of Defense",
        "Awarding Sub Agency": "Defense Logistics Agency",
        "Award Amount": 1000,
        "Base Obligation Date": "2024-06-01",
        "Start Date": "2024-06-02",
        "End Date": "2025-06-01",
        "Description": "NSN 6545-01-632-0167 quantity 10",
        "PSC": {"code": "6515", "description": "MEDICAL"},
        "NAICS": {"code": "423450", "description": "WHOLESALERS"},
    }
    row.update(overrides)
    return row


def test_quantity_and_unit_price_are_not_invented_from_obligation() -> None:
    payload = _search_row(Description="quantity 10 for the kit", **{"Award Amount": 1000})
    quantity, unit_price = derive_quantity_and_unit_price(payload, payload["Description"])
    item = normalize_award(payload)
    assert quantity == Decimal(10)
    assert unit_price is None
    assert item is not None
    assert item.quantity == Decimal(10)
    assert item.unit_price is None
    assert item.total_obligation == Decimal(1000)
    labeled = _search_row(Description="quantity 10 unit price $25.00")
    labeled_item = normalize_award(labeled)
    assert labeled_item is not None
    assert labeled_item.unit_price == Decimal(25)
    assert labeled_item.total_obligation == Decimal(1000)
    bare = normalize_award(_search_row(Description="part 6545016320167 only"))
    assert bare is not None
    assert bare.nsn is None
    assert bare.quantity is None


def test_live_fixture_keeps_obligation_and_extracts_labeled_nsn() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert document["results"]
    item = normalize_award(document["results"][0])
    assert item is not None
    assert item.award_id == FIXTURE_IDS[0]
    assert item.piid == "SPE2DM26FVSMW"
    assert item.nsn == "6545-01-632-0167"
    assert item.psc_code == "6515"
    assert item.naics_code == "423450"
    assert item.recipient_uei == "ZJEUBM5FYLQ2"
    assert item.recipient_name == "CARDINAL HEALTH 200, LLC"
    assert item.awarding_agency == "Department of Defense / Defense Logistics Agency"
    assert item.action_date == date(2026, 6, 11)
    assert item.total_obligation == Decimal("96870.96")
    assert item.quantity is None
    assert item.unit_price is None
    assert item.set_aside_code is None
    explicit = dict(document["results"][0])
    explicit["type_set_aside"] = "NONE"
    assert normalize_award(explicit).set_aside_code == "NONE"


def test_pull_is_idempotent_and_pages_until_has_next_is_false(session: Session) -> None:
    _isolate_watchlists(session)
    create_watchlist(session, name=f"phase5-{uuid4().hex}", psc_codes=["6515"], sources=["usaspending"])
    first = _search_row(generated_internal_id="CONT_AWD_PHASE5_PAGE1")
    second = _search_row(
        generated_internal_id="CONT_AWD_PHASE5_PAGE2",
        Description="NSN 5306-00-111-2222 unit cost $4.50",
        **{"Award Amount": 80},
    )
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        bodies.append(body)
        page = body["page"]
        rows = [first] if page == 1 else [second]
        return httpx.Response(
            200,
            json={
                "results": rows,
                "page_metadata": {"page": page, "hasNext": page == 1},
                "messages": [],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    plan = plan_pull(session, today=date(2026, 9, 26), force_backfill=True)
    try:
        stats = pull_usaspending(session, plan, client=client, limit=1, attempts=1, wait=FAST_WAIT)
        session.flush()
        session.expire_all()
        assert stats.fetched == 2
        assert stats.inserted == 2
        assert [body["page"] for body in bodies] == [1, 2]
        assert bodies[0]["limit"] == 1
        assert bodies[0]["filters"]["award_type_codes"] == ["A", "B", "C", "D"]
        assert bodies[0]["filters"]["time_period"][0]["date_type"] == "action_date"
        assert bodies[0]["filters"]["time_period"][0]["start_date"] == "2023-09-26"
        stored = session.scalars(select(Award).where(Award.award_id.in_([first["generated_internal_id"], second["generated_internal_id"]]))).all()
        by_id = {row.award_id: row for row in stored}
        assert by_id[first["generated_internal_id"]].unit_price is None
        assert by_id[first["generated_internal_id"]].quantity == Decimal(10)
        assert by_id[first["generated_internal_id"]].raw["Award Amount"] in (1000, 1000.0, Decimal(1000))
        assert by_id[second["generated_internal_id"]].nsn == "5306-00-111-2222"
        assert by_id[second["generated_internal_id"]].unit_price == Decimal("4.50")
        assert by_id[second["generated_internal_id"]].total_obligation == Decimal(80)
        again = pull_usaspending(session, plan, client=client, limit=1, attempts=1, wait=FAST_WAIT)
        assert again.fetched == 2
        assert again.inserted == 0
        assert again.updated == 0
        assert again.unchanged == 2
    finally:
        client.close()


def test_changed_payload_updates_without_inventing_price(session: Session) -> None:
    original = _search_row(generated_internal_id="CONT_AWD_PHASE5_CHANGE", **{"Award Amount": 900})
    original["Description"] = "quantity 9"
    stats = ingest_award_records(session, [original])
    assert stats.inserted == 1
    changed = dict(original)
    changed["Description"] = "quantity 9 unit price $12.50"
    changed["Award Amount"] = 900
    updated = ingest_award_records(session, [changed])
    session.expire_all()
    assert updated.updated == 1
    row = session.scalar(select(Award).where(Award.award_id == "CONT_AWD_PHASE5_CHANGE"))
    assert row is not None
    assert row.quantity == Decimal(9)
    assert row.unit_price == Decimal("12.50")
    assert row.total_obligation == Decimal(900)


def test_incremental_pull_uses_last_modified_after_backfill(session: Session) -> None:
    _isolate_watchlists(session)
    run = start_run(session, JOB_NAME)
    finish_run(
        run,
        IngestStats(),
        status="succeeded",
        details={
            "mode": "backfill",
            "date_type": "action_date",
            "window_start": "2023-09-26",
            "window_end": "2026-09-20",
        },
    )
    session.flush()
    create_watchlist(session, name=f"phase5-delta-{uuid4().hex}", naics_codes=["5415"], sources=["usaspending"])
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"results": [], "page_metadata": {"page": 1, "hasNext": False}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        plan = plan_pull(session, today=date(2026, 9, 26))
        assert plan.mode == "incremental"
        assert plan.date_type == "last_modified_date"
        assert plan.start == date(2026, 9, 19)
        assert plan.end == date(2026, 9, 26)
        stats = pull_usaspending(session, plan, client=client, attempts=1, wait=FAST_WAIT)
        assert stats.fetched == 0
        assert len(bodies) == 1
        assert "psc_codes" not in bodies[0]["filters"]
        assert bodies[0]["filters"]["naics_codes"] == {"require": ["5415"]}
        assert bodies[0]["filters"]["time_period"][0]["date_type"] == "last_modified_date"
        assert bodies[0]["filters"]["time_period"][0]["start_date"] == "2026-09-19"
    finally:
        client.close()


def test_no_codes_and_disabled_watchlists_do_not_call_the_network(session: Session) -> None:
    _isolate_watchlists(session)
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json={"results": [], "page_metadata": {"hasNext": False}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    plan = plan_pull(session, today=date(2026, 9, 26), force_backfill=True)
    try:
        skipped = pull_usaspending(session, plan, client=client, attempts=1, wait=FAST_WAIT)
        assert skipped.fetched == 0
        assert calls["count"] == 0
        create_watchlist(
            session,
            name=f"phase5-off-{uuid4().hex}",
            psc_codes=["NOPE"],
            enabled=False,
            sources=["usaspending"],
        )
        create_watchlist(session, name=f"phase5-on-{uuid4().hex}", psc_codes=["R4"], naics_codes=["3364"])
        bodies: list[dict] = []

        def recording(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content.decode()))
            return httpx.Response(200, json={"results": [], "page_metadata": {"page": 1, "hasNext": False}})

        recording_client = httpx.Client(transport=httpx.MockTransport(recording))
        try:
            pull_usaspending(session, plan, client=recording_client, attempts=1, wait=FAST_WAIT)
        finally:
            recording_client.close()
        assert len(bodies) == 2
        psc_body = next(body for body in bodies if "psc_codes" in body["filters"])
        naics_body = next(body for body in bodies if "naics_codes" in body["filters"])
        assert psc_body["filters"]["psc_codes"] == ["R4"]
        assert "naics_codes" not in psc_body["filters"]
        assert naics_body["filters"]["naics_codes"] == {"require": ["3364"]}
        assert "psc_codes" not in naics_body["filters"]
    finally:
        client.close()


def test_repeated_page_fails_the_pull(session: Session) -> None:
    _isolate_watchlists(session)
    create_watchlist(session, name=f"phase5-repeat-{uuid4().hex}", psc_codes=["6515"])
    row = _search_row(generated_internal_id="CONT_AWD_PHASE5_REPEAT")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"results": [row], "page_metadata": {"page": 1, "hasNext": True}},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    plan = plan_pull(session, today=date(2026, 9, 26), force_backfill=True)
    try:
        with pytest.raises(UsaSpendingError, match="repeated award ids"):
            pull_usaspending(session, plan, client=client, attempts=1, wait=FAST_WAIT)
    finally:
        client.close()


def test_known_nsn_history_returns_vendor_date_amount_and_known_unit_price_only(session: Session) -> None:
    _award(
        session,
        award_id="phase5-nsn-priced",
        action_date=date(2025, 1, 15),
        total_obligation=Decimal(999),
        quantity=Decimal(40),
        unit_price=Decimal("12.5"),
        recipient_name="Priced Vendor",
    )
    _award(
        session,
        award_id="phase5-nsn-unpriced",
        action_date=date(2025, 3, 1),
        total_obligation=Decimal(900),
        quantity=Decimal(9),
        unit_price=None,
        recipient_name="Unpriced Vendor",
    )
    _award(session, award_id="phase5-other-nsn", nsn="5305-00-123-4567", recipient_name="Other NSN")
    rows = price_history(session, "6545016320167")
    assert [row.award_id for row in rows] == ["phase5-nsn-unpriced", "phase5-nsn-priced"]
    priced = rows[1]
    unpriced = rows[0]
    assert priced.vendor_name == "Priced Vendor"
    assert priced.action_date == date(2025, 1, 15)
    assert priced.amount == Decimal(999)
    assert priced.unit_price == Decimal("12.5")
    assert unpriced.vendor_name == "Unpriced Vendor"
    assert unpriced.amount == Decimal(900)
    assert unpriced.unit_price is None
    assert unpriced.quantity == Decimal(9)


def test_psc_history_keywords_are_whole_words(session: Session) -> None:
    _award(
        session,
        award_id="phase5-helmet",
        psc_code="6515",
        description="tactical helmet cover",
        nsn=None,
    )
    _award(
        session,
        award_id="phase5-radio",
        psc_code="6515",
        description="unclassified radio",
        nsn=None,
    )
    _award(session, award_id="phase5-other-psc", psc_code="R425", description="tactical helmet", nsn=None)
    helmets = price_history_psc(session, "65", ["helmet"])
    assert [row.award_id for row in helmets] == ["phase5-helmet"]
    assert price_history_psc(session, "6515", ["classified"]) == []
    assert {row.award_id for row in price_history_psc(session, "6515", [])} >= {"phase5-helmet", "phase5-radio"}


def test_agency_history_and_top_awardees(session: Session) -> None:
    _award(
        session,
        award_id="phase5-dla-1",
        recipient_uei="UEI-TOP",
        recipient_name="Top Vendor",
        total_obligation=Decimal(100),
        psc_code="R425",
        nsn=None,
    )
    _award(
        session,
        award_id="phase5-dla-2",
        recipient_uei="UEI-TOP",
        recipient_name="Top Vendor",
        total_obligation=Decimal(50),
        psc_code="R425",
        nsn=None,
    )
    _award(
        session,
        award_id="phase5-navy",
        awarding_agency="Department of the Navy",
        recipient_uei="UEI-NAVY",
        recipient_name="Navy Vendor",
        psc_code="R425",
        nsn=None,
    )
    history = award_history_for_agency(session, "Defense Logistics Agency", psc="R4")
    assert {row.award_id for row in history} == {"phase5-dla-1", "phase5-dla-2"}
    assert award_history_for_agency(session, "Department of the Navy", nsn="6545-01-632-0167") == []
    totals = top_awardees(session, psc="R425")
    assert "unit_price" not in AwardeeTotal.__dataclass_fields__
    top = next(row for row in totals if row.recipient_uei == "UEI-TOP")
    assert top.award_count == 2
    assert top.total_obligation == Decimal(150)
    assert top.recipient_name == "Top Vendor"


def test_recompete_view_keeps_older_awards_and_skips_long_periods(session: Session) -> None:
    views = set(inspect(session.get_bind()).get_view_names())
    assert "award_recompete_candidates" in views
    today = datetime.now(UTC).date()
    old = today - timedelta(days=800)
    recent = today - timedelta(days=30)
    far_end = (today + timedelta(days=800)).isoformat()
    near_end = (today + timedelta(days=40)).isoformat()
    _award(session, award_id="phase5-recompete-open", action_date=old, nsn="9999-00-000-0001", raw={"End Date": None})
    _award(
        session,
        award_id="phase5-recompete-near",
        action_date=old,
        nsn="9999-00-000-0001",
        raw={"End Date": near_end},
    )
    _award(
        session,
        award_id="phase5-recompete-far",
        action_date=old,
        nsn="9999-00-000-0001",
        raw={"End Date": far_end},
    )
    _award(
        session,
        award_id="phase5-recompete-recent",
        action_date=recent,
        nsn="9999-00-000-0001",
        raw={"End Date": near_end},
    )
    rows = recompete_candidates(session, nsn="9999-00-000-0001")
    assert [row.award_id for row in rows] == ["phase5-recompete-near", "phase5-recompete-open"]
    assert {row.heuristic for row in rows} == {"older_award"}
    near = next(row for row in rows if row.award_id == "phase5-recompete-near")
    assert near.period_end == today + timedelta(days=40)
    opened = next(row for row in rows if row.award_id == "phase5-recompete-open")
    assert opened.period_end is None


def test_digest_displays_recent_award_comps(session: Session, tmp_path: Path) -> None:
    session.execute(update(Match).values(alerted_at=datetime(2099, 1, 1, tzinfo=UTC)))
    session.execute(update(Match).where(Match.status == "new").values(status="seen"))
    session.flush()
    watchlist = create_watchlist(session, name=f"phase5-digest-{uuid4().hex}", psc_codes=["6515"], sources=["sam"])
    opportunity = Opportunity(
        source="sam",
        source_id=f"phase5-opp-{uuid4().hex}",
        title="JFAK replenishment",
        agency_path="Defense Logistics Agency",
        psc_code="6515",
        nsn="6545-01-632-0167",
        response_deadline=datetime(2026, 10, 1, 12, tzinfo=UTC),
        links={"ui": "https://sam.gov/opp/phase5/view"},
        raw={"fixture": True},
    )
    session.add(opportunity)
    session.flush()
    session.add(
        Match(
            opportunity_id=opportunity.id,
            watchlist_id=watchlist.id,
            score=Decimal(1),
            matched_on={"score_basis": "rule_hits"},
            status="new",
        )
    )
    _award(
        session,
        award_id="phase5-comp-priced",
        action_date=date(2025, 5, 1),
        recipient_name="Comp Vendor",
        total_obligation=Decimal(999),
        unit_price=Decimal("12.5"),
    )
    _award(
        session,
        award_id="phase5-comp-unpriced",
        action_date=date(2025, 8, 1),
        recipient_name="Obligation Only",
        total_obligation=Decimal(900),
        quantity=Decimal(9),
        unit_price=None,
    )
    result = run_digest(session, settings=_settings(tmp_path), now=NOW)
    assert result.sent is True
    html = next(tmp_path.glob("digest-*.html")).read_text(encoding="utf-8")
    assert "Recent award comps" in html
    assert "Historical awards" not in html
    assert "Bid recommendation" not in html
    priced = html.split('data-award-id="phase5-comp-priced"', 1)[1].split("</li>", 1)[0]
    unpriced = html.split('data-award-id="phase5-comp-unpriced"', 1)[1].split("</li>", 1)[0]
    assert "Comp Vendor" in priced
    assert "2025-05-01" in priced
    assert "999" in priced
    assert "Unit price: 12.5" in priced
    assert "Obligation Only" in unpriced
    assert "2025-08-01" in unpriced
    assert "900" in unpriced
    assert "Unit price" not in unpriced
    assert "100" not in unpriced


def test_cli_file_ingest_and_award_commands(upgraded_engine) -> None:
    first = runner.invoke(app, ["ingest", "usaspending", "--file", str(FIXTURE)])
    assert first.exit_code == 0, first.output
    assert "inserted: 2" in first.stdout
    second = runner.invoke(app, ["ingest", "usaspending", "--file", str(FIXTURE)])
    assert second.exit_code == 0, second.output
    assert "unchanged: 2" in second.stdout
    assert "inserted: 0" in second.stdout
    history = runner.invoke(app, ["awards", "price-history", "--nsn", "6545-01-632-0167"])
    assert history.exit_code == 0, history.output
    assert "CARDINAL HEALTH 200, LLC" in history.stdout
    assert "96870.96" in history.stdout
    assert "unit_price:" not in history.stdout
    tops = runner.invoke(app, ["awards", "top", "--psc", "6515"])
    assert tops.exit_code == 0, tops.output
    assert "award_count: 2" in tops.stdout
    assert "unit_price" not in tops.stdout
    recompete = runner.invoke(app, ["awards", "recompete", "--nsn", "6545-01-632-0167"])
    assert recompete.exit_code == 0, recompete.output
    assert "count: 0" in recompete.stdout
    factory = sessionmaker(bind=upgraded_engine)
    with factory() as cleanup:
        cleanup.execute(delete(Award).where(Award.award_id.in_(FIXTURE_IDS)))
        cleanup.execute(delete(IngestionRun).where(IngestionRun.job == JOB_NAME, IngestionRun.errors["details"]["mode"].astext == "file"))
        cleanup.commit()


def test_load_search_document_reads_the_fixture() -> None:
    rows = load_search_document(FIXTURE)
    assert len(rows) == 2
    assert rows[0]["generated_internal_id"] == FIXTURE_IDS[0]


def test_scheduled_usaspending_step_pulls_and_advances_the_watermark(session: Session, tmp_path) -> None:
    """H1: the scheduler step imports and runs, and records the watermark run."""
    from unittest.mock import patch

    from govcon.ingest.usaspending import JOB_NAME, last_completed_window_end
    from govcon.scheduler.jobs import step_usaspending

    _isolate_watchlists(session)
    create_watchlist(session, name=f"phase5-sched-{uuid4().hex}", psc_codes=["6515"], sources=["usaspending"])
    row = _search_row(generated_internal_id=f"CONT_AWD_SCHED_{uuid4().hex}")
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"results": [row], "page_metadata": {"hasNext": False}, "messages": []})

    def fake_client(*_args, **_kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler))

    with patch("govcon.ingest.usaspending.build_client", side_effect=fake_client):
        first = step_usaspending(session, _settings(tmp_path))
        assert first.status == "succeeded", first.error
        assert first.inserted == 1
        run = session.get(IngestionRun, first.extra["run_id"])
        assert run.job == JOB_NAME
        assert run.errors["details"]["mode"] in {"backfill", "incremental"}
        assert last_completed_window_end(session) is not None

        second = step_usaspending(session, _settings(tmp_path))
        assert second.status == "succeeded", second.error
        assert second.extra["mode"] == "incremental"
        assert bodies[-1]["filters"]["time_period"][0]["date_type"] == "last_modified_date"


def test_scheduled_usaspending_step_reports_failure_without_raising(session: Session, tmp_path) -> None:
    from unittest.mock import patch

    from govcon.scheduler.jobs import step_usaspending

    _isolate_watchlists(session)
    create_watchlist(session, name=f"phase5-schedfail-{uuid4().hex}", psc_codes=["6515"], sources=["usaspending"])

    def fake_client(*_args, **_kwargs):
        return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(400, json={"detail": "bad"})))

    with patch("govcon.ingest.usaspending.build_client", side_effect=fake_client):
        result = step_usaspending(session, _settings(tmp_path))
    assert result.status == "failed"
    assert "HTTP 400" in (result.error or "")


def _history_run(session: Session, *, mode: str = "incremental", **codes) -> None:
    run = start_run(session, JOB_NAME)
    details = {"mode": mode, "date_type": "last_modified_date", "window_start": "2026-09-18", "window_end": "2026-09-20"}
    details.update(codes)
    finish_run(run, IngestStats(), status="succeeded", details=details)
    session.flush()


def _capture() -> tuple[httpx.Client, list[dict]]:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"results": [], "page_metadata": {"page": 1, "hasNext": False}})

    return httpx.Client(transport=httpx.MockTransport(handler)), bodies


def test_new_watchlist_codes_get_their_three_year_history(session: Session) -> None:
    """M4: the incremental pull also backfills codes no earlier pull covered."""
    _isolate_watchlists(session)
    _history_run(session, psc_codes=["R4"], naics_codes=[])
    create_watchlist(session, name=f"m4-old-{uuid4().hex}", psc_codes=["R4", "R425"], sources=["usaspending"])
    create_watchlist(session, name=f"m4-new-{uuid4().hex}", psc_codes=["6515"], naics_codes=["339112"], sources=["usaspending"])

    plan = plan_pull(session, today=date(2026, 9, 26))
    assert plan.mode == "incremental"
    # R425 sits under the covered R4 prefix; 6515 and 339112 are new.
    assert plan.backfill_psc_codes == ("6515",)
    assert plan.backfill_naics_codes == ("339112",)
    assert (plan.backfill_start, plan.backfill_end) == (date(2023, 9, 26), date(2026, 9, 26))

    client, bodies = _capture()
    try:
        pull_usaspending(session, plan, client=client, attempts=1, wait=FAST_WAIT)
    finally:
        client.close()
    periods = [(b["filters"]["time_period"][0]["date_type"], b["filters"].get("psc_codes"), b["filters"].get("naics_codes")) for b in bodies]
    assert ("last_modified_date", ["R4", "R425", "6515"], None) in periods
    assert ("last_modified_date", None, {"require": ["339112"]}) in periods
    assert ("action_date", ["6515"], None) in periods
    assert ("action_date", None, {"require": ["339112"]}) in periods
    backfill = next(b for b in bodies if b["filters"]["time_period"][0]["date_type"] == "action_date")
    assert backfill["filters"]["time_period"][0]["start_date"] == "2023-09-26"

    details = plan_details(plan, trigger="scheduler")
    assert details["psc_codes"] == ["R4", "R425", "6515"] and details["naics_codes"] == ["339112"]
    assert details["backfilled_psc_codes"] == ["6515"]
    # Once recorded, the next incremental pull has nothing new to backfill.
    _history_run(session, psc_codes=details["psc_codes"], naics_codes=details["naics_codes"])
    again = plan_pull(session, today=date(2026, 9, 27))
    assert again.backfill_psc_codes == () and again.backfill_naics_codes == () and again.backfill_start is None


def test_runs_from_before_codes_were_recorded_do_not_trigger_backfill(session: Session) -> None:
    _isolate_watchlists(session)
    _history_run(session)  # legacy details: no codes recorded
    create_watchlist(session, name=f"m4-legacy-{uuid4().hex}", psc_codes=["6515"], sources=["usaspending"])
    plan = plan_pull(session, today=date(2026, 9, 26))
    assert plan.mode == "incremental" and plan.backfill_psc_codes == ()


def test_bare_labeled_nsn_is_parsed_from_award_descriptions() -> None:
    award = normalize_award(_search_row(Description="BRACKET NSN 5340012345678 QTY 4"))
    assert award is not None and award.nsn == "5340-01-234-5678"
    # An unlabeled 13-digit run is not taken as an NSN.
    unlabeled = normalize_award(_search_row(Description="CONTRACT REF 5340012345678"))
    assert unlabeled is not None and unlabeled.nsn is None
