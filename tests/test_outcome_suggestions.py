"""Roadmap stage 5: award-match outcome suggestions (ADR-073) and analytics refresh (ADR-074)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_web_ui import _make_user
from web_client import CsrfTestClient

from govcon.db import session_scope
from govcon.models import (
    AnalyticsSnapshot,
    Award,
    Opportunity,
    OutcomeFeedback,
    OutcomeSuggestion,
    Pursuit,
    Submission,
)
from govcon.web.app import create_app

OUR_UEI = "OURUEI123456"
SUBMITTED = datetime.now(UTC) - timedelta(days=20)
AWARD_DATE = (SUBMITTED + timedelta(days=5)).date()  # awards follow submission


@pytest.fixture()
def db(upgraded_engine):
    with Session(upgraded_engine) as session:
        yield session


@pytest.fixture()
def our_uei(monkeypatch):
    monkeypatch.setenv("COMPANY_UEI", OUR_UEI)
    from govcon.config import get_settings
    get_settings.cache_clear()
    yield OUR_UEI
    get_settings.cache_clear()


def unique_nsn() -> str:
    digits = f"{uuid4().int % 10**9:09d}"
    return f"6515-01-{digits[:3]}-{digits[3:7]}"


def submitted_bid(db, *, solicitation=None, nsn=None):
    nsn = nsn or unique_nsn()  # awards from other tests must not match this bid
    opp = Opportunity(source="sam", source_id=uuid4().hex, title="Gloves", status="closed",
                      solicitation_number=solicitation or f"SPE-{uuid4().hex[:8].upper()}", nsn=nsn,
                      psc_code="6515", agency_path="DEPT OF DEFENSE.DEFENSE LOGISTICS AGENCY.DLA TROOP SUPPORT",
                      raw={}, links={})
    db.add(opp)
    db.flush()
    pursuit = Pursuit(opportunity_id=opp.id, stage="submitted", submitted_at=SUBMITTED)
    db.add(pursuit)
    db.flush()
    db.add(Submission(opportunity_id=opp.id, pursuit_id=pursuit.id, status="submitted", submitted_at=SUBMITTED))
    db.commit()
    return opp


def award_notice(db, opp, *, awardee_uei, name="Some Vendor", piid=None):
    notice = Opportunity(source="sam", source_id=uuid4().hex, title="Award: gloves", status="closed",
                         opportunity_type="Award Notice", solicitation_number=f" {opp.solicitation_number} ",
                         raw={"award": {"date": AWARD_DATE.isoformat(), "number": piid or f"SPE-C-{uuid4().hex[:6]}",
                                        "amount": "12500.00", "awardee": {"name": name, "ueiSAM": awardee_uei}}},
                         links={})
    db.add(notice)
    db.commit()
    return notice


def run(settings_uei=True):
    from govcon.learning.award_matching import suggest_outcomes
    with session_scope() as s:
        return suggest_outcomes(s)


def suggestions(db, opp):
    db.expire_all()
    return db.scalars(select(OutcomeSuggestion).where(OutcomeSuggestion.opportunity_id == opp.id)
                      .order_by(OutcomeSuggestion.id)).all()


@pytest.mark.parametrize("awardee,expected", [(OUR_UEI, "won"), ("OTHERVENDOR1", "lost"), (None, None)])
def test_award_notice_with_our_solicitation_number_is_a_strong_suggestion(db, our_uei, awardee, expected):
    opp = submitted_bid(db)
    award_notice(db, opp, awardee_uei=awardee)
    run()
    [s] = suggestions(db, opp)
    assert (s.source, s.strength, s.suggested_outcome) == ("sam_award_notice", "strong", expected)
    assert s.matched_identifiers["solicitation_number"] == opp.solicitation_number
    assert s.award_amount == Decimal("12500.00") and s.award_date == AWARD_DATE
    run()
    assert len(suggestions(db, opp)) == 1, "suggestions are not duplicated"


def test_without_our_uei_no_outcome_is_suggested(db, monkeypatch):
    monkeypatch.delenv("COMPANY_UEI", raising=False)
    from govcon.config import get_settings
    get_settings.cache_clear()
    opp = submitted_bid(db)
    award_notice(db, opp, awardee_uei="OTHERVENDOR1")
    run()
    [s] = suggestions(db, opp)
    assert s.strength == "strong" and s.suggested_outcome is None


def test_missing_award_data_never_suggests_a_loss(db, our_uei):
    opp = submitted_bid(db)
    run()
    assert suggestions(db, opp) == []
    db.expire_all()
    assert db.scalar(select(OutcomeFeedback).where(OutcomeFeedback.opportunity_id == opp.id)) is None


def test_usaspending_awards_are_possible_or_strong_by_identifier(db, our_uei):
    nsn = unique_nsn()
    opp = submitted_bid(db, nsn=nsn)
    piid = f"SPE2D1-{uuid4().hex[:6].upper()}"
    award_notice(db, opp, awardee_uei="OTHERVENDOR1", piid=piid)
    db.add_all([
        Award(award_id=f"linked-{uuid4().hex}", piid=piid, recipient_uei="OTHERVENDOR1", recipient_name="Other",
              awarding_agency="Department of Defense / Defense Logistics Agency", action_date=date(2026, 9, 2),
              total_obligation=Decimal(12500), raw={}),
        Award(award_id=f"same-nsn-{uuid4().hex}", nsn=nsn, recipient_uei="THIRDVENDOR1",
              awarding_agency="Department of Defense / Defense Logistics Agency",
              action_date=(SUBMITTED + timedelta(days=10)).date(), raw={}),
        Award(award_id=f"other-agency-{uuid4().hex}", nsn=nsn, recipient_uei="FOURTHVENDOR",
              awarding_agency="Department of Veterans Affairs", action_date=(SUBMITTED + timedelta(days=10)).date(),
              raw={}),
    ])
    db.commit()
    run()
    by_ref = {s.source_ref: s for s in suggestions(db, opp)}
    linked = next(s for ref, s in by_ref.items() if ref.startswith("linked-"))
    same = next(s for ref, s in by_ref.items() if ref.startswith("same-nsn-"))
    assert (linked.strength, linked.suggested_outcome) == ("strong", "lost")
    assert (same.strength, same.suggested_outcome) == ("possible", None), "a possible match suggests no outcome"
    assert not any(ref.startswith("other-agency-") for ref in by_ref)


def test_a_person_confirms_or_dismisses_the_suggestion(db, our_uei):
    opp = submitted_bid(db)
    award_notice(db, opp, awardee_uei=OUR_UEI, name="Our Company")
    other = submitted_bid(db)
    award_notice(db, other, awardee_uei="OTHERVENDOR1")
    run()
    [won] = suggestions(db, opp)
    [lost] = suggestions(db, other)
    _, reviewer_token = _make_user(db, f"sug-rev-{uuid4().hex}@example.test", "reviewer")
    _, token = _make_user(db, f"sug-appr-{uuid4().hex}@example.test", "approver")
    with CsrfTestClient(create_app(), follow_redirects=False) as client:
        client.cookies.set("govcon_session", reviewer_token)
        denied = client.post(f"/workspace/{opp.id}/outcome-suggestions/{won.id}/confirm")
        assert "error=" in denied.headers["location"], "only an approver records an outcome"
        client.cookies.set("govcon_session", token)
        page = client.get(f"/workspace/{opp.id}?tab=submission").text
        assert "Strong match" in page and "Confirm: record won" in page
        ok = client.post(f"/workspace/{opp.id}/outcome-suggestions/{won.id}/confirm")
        assert "notice=" in ok.headers["location"], ok.headers["location"]
        client.post(f"/workspace/{other.id}/outcome-suggestions/{lost.id}/dismiss")
    db.expire_all()
    assert db.get(Pursuit, db.scalar(select(Pursuit.id).where(Pursuit.opportunity_id == opp.id))).stage == "won"
    feedback = db.scalar(select(OutcomeFeedback).where(OutcomeFeedback.opportunity_id == opp.id))
    assert feedback.outcome == "won" and feedback.awarded_vendor_uei == OUR_UEI
    assert db.get(OutcomeSuggestion, won.id).status == "confirmed"
    assert db.get(OutcomeSuggestion, lost.id).status == "dismissed"
    assert db.get(Pursuit, db.scalar(select(Pursuit.id).where(Pursuit.opportunity_id == other.id))).stage == "submitted"


# ── analytics refresh ────────────────────────────────────────────────────────

def test_analytics_refresh_records_what_it_counted(db, monkeypatch):
    from govcon.learning import analytics
    from govcon.scheduler.jobs import step_analytics_refresh
    real = analytics.outcome_analytics
    empty = SimpleNamespace(total_won=0, total_lost=0, total_no_bid=0)
    monkeypatch.setattr(analytics, "outcome_analytics", lambda session: empty)
    with session_scope() as s:
        result = step_analytics_refresh(s)
    assert result.status == "skipped" and result.extra["reason"] == "no recorded outcomes"
    monkeypatch.setattr(analytics, "outcome_analytics", real)
    opp = submitted_bid(db)
    db.add(OutcomeFeedback(opportunity_id=opp.id, outcome="won"))
    db.commit()
    with session_scope() as s:
        result = step_analytics_refresh(s)
    assert result.status == "succeeded" and result.inserted == 1 and result.extra["won"] >= 1
    snapshot = db.scalar(select(AnalyticsSnapshot).order_by(AnalyticsSnapshot.id.desc()).limit(1))
    assert snapshot.won == result.extra["won"] and snapshot.payload["total_won"] == snapshot.won


def test_usaspending_chain_runs_outcome_suggestions():
    from govcon.scheduler.chains import CHAIN_DEFINITIONS
    chain = CHAIN_DEFINITIONS["usaspending"]
    assert chain.steps == ["usaspending", "outcome_suggestions"] and "outcome_suggestions" in chain.soft_steps
