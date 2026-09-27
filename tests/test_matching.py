"""Phase 2 watchlist matching engine tests (table-driven)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from govcon.cli import app
from govcon.matching.engine import evaluate_match, run_matching
from govcon.matching.watchlists import create_watchlist
from govcon.models import Match, Opportunity, OpportunityEvent, Watchlist
from govcon.seed import DEMO_WATCHLIST_NAME

runner = CliRunner()
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


@pytest.fixture
def session(upgraded_engine) -> Session:
    factory = sessionmaker(bind=upgraded_engine, autoflush=False, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def _make_opportunity(
    session: Session,
    *,
    source: str = "sam",
    psc_code: str | None = None,
    naics_code: str | None = None,
    title: str | None = None,
    description: str | None = None,
    nsn: str | None = None,
    set_aside_code: str | None = None,
    estimated_value_min: Decimal | None = None,
    estimated_value_max: Decimal | None = None,
    response_deadline: datetime | None = None,
) -> Opportunity:
    row = Opportunity(
        source=source,
        source_id=f"test-{uuid4()}",
        title=title,
        description=description,
        psc_code=psc_code,
        naics_code=naics_code,
        nsn=nsn,
        set_aside_code=set_aside_code,
        estimated_value_min=estimated_value_min,
        estimated_value_max=estimated_value_max,
        response_deadline=response_deadline,
        raw={"fixture": True},
    )
    session.add(row)
    session.flush()
    return row


def _make_watchlist(
    session: Session,
    *,
    name: str | None = None,
    psc_codes: list[str] | None = None,
    naics_codes: list[str] | None = None,
    keywords: list[str] | None = None,
    exclude_keywords: list[str] | None = None,
    max_value: Decimal | None = None,
    min_value: Decimal | None = None,
    sources: list[str] | None = ["sam"],
) -> Watchlist:
    return create_watchlist(
        session,
        name=name or f"watchlist-{uuid4()}",
        psc_codes=psc_codes,
        naics_codes=naics_codes,
        keywords=keywords,
        exclude_keywords=exclude_keywords,
        max_value=max_value,
        min_value=min_value,
        sources=sources,
    )


def _purge_test_matches(session: Session) -> None:
    test_opportunity_ids = select(Opportunity.id).where(Opportunity.source_id.like("test-%"))
    test_watchlist_ids = select(Watchlist.id).where(
        Watchlist.name.like("watchlist-%") | Watchlist.name.like("cli-watchlist-%")
    )
    session.execute(
        delete(Match).where(
            Match.opportunity_id.in_(test_opportunity_ids) | Match.watchlist_id.in_(test_watchlist_ids)
        )
    )
    session.execute(delete(OpportunityEvent).where(OpportunityEvent.opportunity_id.in_(test_opportunity_ids)))
    session.execute(delete(Opportunity).where(Opportunity.source_id.like("test-%")))
    session.execute(
        delete(Watchlist).where(
            Watchlist.name.like("watchlist-%") | Watchlist.name.like("cli-watchlist-%")
        )
    )
    demo = session.scalar(select(Watchlist).where(Watchlist.name == DEMO_WATCHLIST_NAME))
    if demo is not None:
        demo.enabled = False
    session.commit()


MATCH_CASES = [
    pytest.param(
        {
            "watchlist": {"psc_codes": ["R4"]},
            "opportunity": {"psc_code": "R425"},
            "expected": True,
            "group": "psc",
        },
        id="psc_prefix_pass",
    ),
    pytest.param(
        {
            "watchlist": {"psc_codes": ["R4"]},
            "opportunity": {"psc_code": "D399"},
            "expected": False,
            "group": "psc",
        },
        id="psc_prefix_fail",
    ),
    pytest.param(
        {
            "watchlist": {"naics_codes": ["541"]},
            "opportunity": {"naics_code": "541330"},
            "expected": True,
            "group": "naics",
        },
        id="naics_prefix_pass",
    ),
    pytest.param(
        {
            "watchlist": {"naics_codes": ["541"]},
            "opportunity": {"naics_code": "236220"},
            "expected": False,
            "group": "naics",
        },
        id="naics_prefix_fail",
    ),
    pytest.param(
        {
            "watchlist": {"keywords": ["cyber"], "exclude_keywords": ["classified"]},
            "opportunity": {"title": "Cyber operations support", "description": "unclassified"},
            "expected": True,
            "group": "exclude_keywords",
        },
        id="exclude_veto_absent",
    ),
    pytest.param(
        {
            "watchlist": {"keywords": ["cyber"], "exclude_keywords": ["classified"]},
            "opportunity": {"title": "Cyber classified systems"},
            "expected": False,
            "group": "exclude_keywords",
        },
        id="exclude_veto_hit",
    ),
    pytest.param(
        {
            "watchlist": {"psc_codes": [], "naics_codes": ["541"]},
            "opportunity": {"psc_code": "ZZ99", "naics_code": "541512"},
            "expected": True,
            "group": "wildcard",
        },
        id="wildcard_empty_group",
    ),
    pytest.param(
        {
            "watchlist": {"max_value": Decimal("1000000")},
            "opportunity": {"estimated_value_min": None, "estimated_value_max": None},
            "expected": True,
            "group": "value",
            "value_status": "unknown",
        },
        id="unknown_estimated_value",
    ),
    pytest.param(
        {
            "watchlist": {"max_value": Decimal("100000")},
            "opportunity": {"estimated_value_min": Decimal("150000"), "estimated_value_max": Decimal("200000")},
            "expected": False,
            "group": "value",
            "value_status": "fail",
        },
        id="known_value_above_max",
    ),
]


@pytest.mark.parametrize("case", MATCH_CASES)
def test_matching_cases(session: Session, case: dict) -> None:
    _purge_test_matches(session)
    watchlist = _make_watchlist(session, **case["watchlist"])
    opportunity = _make_opportunity(session, **case["opportunity"])
    is_match, matched_on, score = evaluate_match(watchlist, opportunity, now=NOW)

    assert is_match is case["expected"]
    assert score >= Decimal(0)
    assert "groups" in matched_on

    group = case["group"]
    if group == "psc":
        assert matched_on["groups"]["psc"]["matched_prefix"] == ("R4" if case["expected"] else None)
    elif group == "naics":
        assert matched_on["groups"]["naics"]["matched_prefix"] == ("541" if case["expected"] else None)
    elif group == "exclude_keywords":
        assert matched_on["groups"]["exclude_keywords"]["status"] == ("pass" if case["expected"] else "fail")
    elif group == "wildcard":
        assert matched_on["groups"]["psc"]["status"] == "wildcard"
        assert matched_on["groups"]["naics"]["status"] == "pass"
    elif group == "value":
        assert matched_on["groups"]["value"]["status"] == case["value_status"]


def test_idempotent_match_upsert(session: Session) -> None:
    _purge_test_matches(session)
    watchlist = _make_watchlist(session, psc_codes=["R4"])
    _make_opportunity(session, psc_code="R425", title="Network gear")

    stats_first = run_matching(session, watchlist_id=watchlist.id, now=NOW)
    session.commit()
    count_first = session.scalar(
        select(func.count()).select_from(Match).where(Match.watchlist_id == watchlist.id)
    ) or 0

    stats_second = run_matching(session, watchlist_id=watchlist.id, now=NOW)
    session.commit()
    count_second = session.scalar(
        select(func.count()).select_from(Match).where(Match.watchlist_id == watchlist.id)
    ) or 0

    assert count_first == 1
    assert count_second == 1
    assert stats_first.inserted == 1
    assert stats_second.unchanged == 1
    assert stats_second.inserted == 0


def test_match_run_cli(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _purge_test_matches(session)
    watchlist = _make_watchlist(session, psc_codes=["R4"], name=f"watchlist-{uuid4()}")
    _make_opportunity(session, psc_code="R425")
    session.commit()

    result = runner.invoke(app, ["match", "run"])
    assert result.exit_code == 0
    assert "inserted:" in result.stdout
    assert session.scalar(select(func.count()).select_from(Match).where(Match.watchlist_id == watchlist.id)) == 1


def test_watchlist_cli_add_list_disable(session: Session) -> None:
    _purge_test_matches(session)
    name = f"cli-watchlist-{uuid4()}"
    add = runner.invoke(
        app,
        [
            "watchlist",
            "add",
            "--name",
            name,
            "--psc-codes",
            "R4",
            "--naics-codes",
            "541",
            "--keywords",
            "network",
        ],
    )
    assert add.exit_code == 0
    watchlist_id = int(add.stdout.split("watchlist_id: ")[1].splitlines()[0])

    listing = runner.invoke(app, ["watchlist", "list"])
    assert listing.exit_code == 0
    assert name in listing.stdout
    assert "R4" in listing.stdout

    disable = runner.invoke(app, ["watchlist", "disable", "--watchlist-id", str(watchlist_id)])
    assert disable.exit_code == 0

    row = session.get(Watchlist, watchlist_id)
    assert row is not None
    assert row.enabled is False


def test_match_rebuild_removes_stale_matches(session: Session) -> None:
    _purge_test_matches(session)
    watchlist = _make_watchlist(session, psc_codes=["R4"])
    matching = _make_opportunity(session, psc_code="R425")
    stale = _make_opportunity(session, psc_code="D399")
    session.commit()

    run_matching(session, watchlist_id=watchlist.id, now=NOW, rebuild=True)
    session.commit()
    assert session.scalar(select(func.count()).select_from(Match).where(Match.watchlist_id == watchlist.id)) == 1

    matching.psc_code = "D399"
    stale.psc_code = "R425"
    session.flush()
    session.commit()

    stats = run_matching(session, watchlist_id=watchlist.id, now=NOW, rebuild=True)
    session.commit()
    assert stats.removed >= 1
    remaining_ids = session.scalars(
        select(Match.opportunity_id).where(Match.watchlist_id == watchlist.id)
    ).all()
    assert remaining_ids == [stale.id]
