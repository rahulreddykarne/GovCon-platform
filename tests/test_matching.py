"""Phase 2 watchlist matching acceptance tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker
from typer.testing import CliRunner

from govcon.cli import app
from govcon.matching.dedup import upsert_match
from govcon.matching.engine import rebuild_watchlist, run_matching
from govcon.matching.rules import evaluate_watchlist
from govcon.matching.watchlists import create_watchlist
from govcon.models import Match, Opportunity, Watchlist

runner = CliRunner()


@pytest.fixture
def isolated_watchlists(upgraded_engine):
    factory = sessionmaker(bind=upgraded_engine, expire_on_commit=False)
    with factory() as session:
        for row in session.scalars(select(Watchlist)).all():
            row.enabled = False
        for row in session.scalars(select(Match)).all():
            session.delete(row)
        session.commit()
    yield upgraded_engine


def _opportunity(**overrides) -> Opportunity:
    defaults = {
        "source": "sam",
        "source_id": uuid4().hex,
        "raw": {"fixture": True},
        "title": "Sample opportunity",
        "description": "General services contract",
        "psc_code": "R4250",
        "naics_code": "541512",
        "set_aside_code": "SBA",
        "status": "open",
    }
    defaults.update(overrides)
    return Opportunity(**defaults)


def _watchlist(**overrides) -> Watchlist:
    defaults = {
        "name": f"watchlist-{uuid4().hex[:8]}",
        "enabled": True,
        "psc_codes": [],
        "naics_codes": [],
        "keywords": [],
        "exclude_keywords": [],
        "nsn_list": [],
        "set_asides": [],
        "sources": ["sam"],
    }
    defaults.update(overrides)
    return Watchlist(**defaults)


def test_psc_prefix_match() -> None:
    watchlist = _watchlist(psc_codes=["R42", "D3"])
    opportunity = _opportunity(psc_code="R4250")
    result = evaluate_watchlist(opportunity, watchlist)
    assert result.matches is True
    assert result.matched_on["psc"]["matched_prefix"] == "R42"
    assert result.matched_on["psc"]["result"] == "pass"

    mismatch = _opportunity(psc_code="J010")
    assert evaluate_watchlist(mismatch, watchlist).matches is False


def test_naics_prefix_match() -> None:
    watchlist = _watchlist(naics_codes=["541"])
    opportunity = _opportunity(naics_code="541512")
    result = evaluate_watchlist(opportunity, watchlist)
    assert result.matches is True
    assert result.matched_on["naics"]["matched_prefix"] == "541"

    mismatch = _opportunity(naics_code="336411")
    assert evaluate_watchlist(mismatch, watchlist).matches is False


def test_exclude_keyword_veto() -> None:
    watchlist = _watchlist(keywords=["services"], exclude_keywords=["classified"])
    pass_opp = _opportunity(title="Professional services", description="Open market")
    veto_opp = _opportunity(title="Classified services support", description="Secret program")
    assert evaluate_watchlist(pass_opp, watchlist).matches is True
    veto = evaluate_watchlist(veto_opp, watchlist)
    assert veto.matches is False
    assert veto.vetoed is True
    assert veto.matched_on["exclude_keywords"]["result"] == "veto"


def test_wildcard_empty_group() -> None:
    watchlist = _watchlist(sources=["sam"])
    opportunity = _opportunity(source="sam", psc_code=None, naics_code=None)
    result = evaluate_watchlist(opportunity, watchlist)
    assert result.matches is True
    assert "psc" not in result.matched_on
    assert "naics" not in result.matched_on
    assert result.matched_on["sources"]["result"] == "pass"


def test_unknown_estimated_value_does_not_reject() -> None:
    watchlist = _watchlist(max_value=Decimal("1000000"))
    opportunity = _opportunity(estimated_value_min=None, estimated_value_max=None)
    result = evaluate_watchlist(opportunity, watchlist)
    assert result.matches is True
    assert result.matched_on["value"]["max_value"]["result"] == "unknown"

    priced = _opportunity(estimated_value_min=Decimal("50000"), estimated_value_max=Decimal("75000"))
    assert evaluate_watchlist(priced, watchlist).matches is True
    over = _opportunity(estimated_value_min=Decimal("2000000"), estimated_value_max=Decimal("2500000"))
    assert evaluate_watchlist(over, watchlist).matches is False


def test_idempotent_match_upsert(isolated_watchlists) -> None:
    upgraded_engine = isolated_watchlists
    factory = sessionmaker(bind=upgraded_engine, expire_on_commit=False)
    with factory() as session:
        watchlist = create_watchlist(session, name="upsert-test", psc_codes=["R42"], sources=["sam"])
        opportunity = _opportunity(psc_code="R4250")
        session.add(opportunity)
        session.commit()
        session.refresh(watchlist)
        session.refresh(opportunity)

        first, created_first = upsert_match(
            session,
            opportunity_id=opportunity.id,
            watchlist_id=watchlist.id,
            score=Decimal("1"),
            matched_on={"psc": {"result": "pass"}},
        )
        second, created_second = upsert_match(
            session,
            opportunity_id=opportunity.id,
            watchlist_id=watchlist.id,
            score=Decimal("2"),
            matched_on={"psc": {"result": "pass", "updated": True}},
        )
        session.commit()
        session.refresh(second)
        count = session.scalar(
            select(func.count()).select_from(Match).where(Match.watchlist_id == watchlist.id)
        )
        assert created_first is True
        assert created_second is False
        assert count == 1
        assert first.id == second.id
        assert second.score == Decimal("2")
        assert second.matched_on["psc"]["updated"] is True


def test_match_run_and_rebuild(isolated_watchlists) -> None:
    upgraded_engine = isolated_watchlists
    factory = sessionmaker(bind=upgraded_engine, expire_on_commit=False)
    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    source = f"test-{uuid4().hex[:8]}"
    with factory() as session:
        watchlist = create_watchlist(session, name="run-test", psc_codes=["R42"], sources=[source])
        match_opp = _opportunity(source=source, psc_code="R4250", title="Radar maintenance")
        miss_opp = _opportunity(source=source, psc_code="J010", title="Janitorial")
        session.add_all([match_opp, miss_opp])
        session.commit()
        session.refresh(watchlist)
        session.refresh(match_opp)
        session.refresh(miss_opp)

        run_matching(session, watchlist_id=watchlist.id, now=now)
        session.commit()
        matches = session.scalars(select(Match).where(Match.watchlist_id == watchlist.id)).all()
        assert len(matches) == 1
        assert matches[0].opportunity_id == match_opp.id
        assert matches[0].matched_on["psc"]["result"] == "pass"

        match_opp.psc_code = "J010"
        miss_opp.psc_code = "R4299"
        session.commit()
        rebuild_watchlist(session, watchlist.id, now=now)
        session.commit()
        remaining = session.scalars(select(Match).where(Match.watchlist_id == watchlist.id)).all()
        assert len(remaining) == 1
        assert remaining[0].opportunity_id == miss_opp.id


def test_match_run_is_idempotent(isolated_watchlists) -> None:
    upgraded_engine = isolated_watchlists
    factory = sessionmaker(bind=upgraded_engine, expire_on_commit=False)
    source = f"test-{uuid4().hex[:8]}"
    with factory() as session:
        watchlist = create_watchlist(session, name="idempotent-run", sources=[source])
        session.add(_opportunity(source=source))
        session.commit()
        session.refresh(watchlist)
        first = run_matching(session, watchlist_id=watchlist.id)
        second = run_matching(session, watchlist_id=watchlist.id)
        session.commit()
        count = session.scalar(
            select(func.count()).select_from(Match).where(Match.watchlist_id == watchlist.id)
        )
    assert first.matches_created == 1
    assert second.matches_created == 0
    assert second.matches_updated == 1
    assert count == 1


def test_cli_watchlist_and_match_commands(isolated_watchlists) -> None:
    upgraded_engine = isolated_watchlists
    source = f"test-{uuid4().hex[:8]}"
    add = runner.invoke(
        app,
        [
            "watchlist",
            "add",
            "--name",
            "CLI Test",
            "--psc",
            "R42",
            "--source",
            source,
        ],
    )
    assert add.exit_code == 0
    assert "watchlist_id:" in add.stdout

    listed = runner.invoke(app, ["watchlist", "list"])
    assert listed.exit_code == 0
    assert "CLI Test" in listed.stdout

    watchlist_id = int(add.stdout.split("watchlist_id:")[1].splitlines()[0].strip())
    factory = sessionmaker(bind=upgraded_engine, expire_on_commit=False)
    with factory() as session:
        session.add(_opportunity(source=source, psc_code="R4250"))
        session.commit()

    match = runner.invoke(app, ["match", "rebuild", "--watchlist", str(watchlist_id)])
    assert match.exit_code == 0
    assert "matches_created: 1" in match.stdout

    disable = runner.invoke(app, ["watchlist", "disable", "--watchlist-id", str(watchlist_id)])
    assert disable.exit_code == 0
    assert f"watchlist_disabled: {watchlist_id}" in disable.stdout

    edit = runner.invoke(
        app,
        ["watchlist", "edit", "--watchlist-id", str(watchlist_id), "--keyword", "radar", "--enable"],
    )
    assert edit.exit_code == 0

    rebuild = runner.invoke(app, ["match", "rebuild", "--watchlist", str(watchlist_id)])
    assert rebuild.exit_code == 0


def test_help_shows_match_and_watchlist_groups() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "match" in result.stdout
    assert "watchlist" in result.stdout
    match_help = runner.invoke(app, ["match", "--help"])
    assert match_help.exit_code == 0
    assert "run" in match_help.stdout
    assert "rebuild" in match_help.stdout
