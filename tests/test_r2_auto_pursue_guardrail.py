"""Audit round 2, finding 5: the auto-pursue guardrail ignores the always-1.0 rule_match factor.

A match exists only when every active rule group passes, so ``rule_match`` is
1.0 for every ranked match. The inbox rank keeps it (unchanged display); the
automatic-pursuit guardrail judges data coverage and score on the other factors.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_ranking_notifications import (  # noqa: F401
    default_settings,
    opportunity,
    rule_match,
    set_setting,
)

from govcon.db import session_scope
from govcon.matching.auto_pursue import (
    needs_eligibility_decision,
    pursued_today,
    run_auto_pursue,
)
from govcon.matching.ranking import rank_match
from govcon.models import Match, Pursuit, Recommendation


@pytest.fixture()
def db(upgraded_engine):
    with Session(upgraded_engine) as session:
        yield session


POLICY = {"enabled": True, "min_score": 75, "min_days": 10}


def _enable(db):
    with session_scope() as s:
        already = pursued_today(s, datetime.now(UTC))
    policy = {**POLICY, "max_per_day": min(50, already + 10)}
    set_setting(db, "auto_pursue", policy)
    return policy


def _ranked(db, opp, *, semantic=None, wins=None):
    match = rule_match(db, opp)
    if semantic is not None:
        db.add(Recommendation(opportunity_id=opp.id, watchlist_id=match.watchlist_id, category="semantic_match",
                              similarity=Decimal(str(semantic)), evidence={}, active=True))
    if wins is not None:
        db.add(Recommendation(opportunity_id=opp.id, category="similar_to_won", similarity=Decimal(str(wins)),
                              evidence={}, active=True))
    db.flush()
    match.rank_score, match.rank_factors = rank_match(db, match, opp, now=datetime.now(UTC))
    db.commit()
    return match


def _run():
    with session_scope() as s:
        return run_auto_pursue(s)


def _pursued(db, opp) -> bool:
    db.expire_all()
    return db.scalar(select(Pursuit).where(Pursuit.opportunity_id == opp.id)) is not None


# ── current behaviour that must not change ───────────────────────────────────

def test_inbox_rank_for_value_and_deadline_only_is_unchanged(db):
    match = _ranked(db, opportunity(db, days=25, value=Decimal(100000)))
    by_name = {f["name"]: f for f in match.rank_factors}
    assert float(match.rank_score) == pytest.approx(81.8, abs=0.05)
    assert by_name["rule_match"]["points"] == pytest.approx(54.5, abs=0.1)
    assert [f["name"] for f in match.rank_factors if f["available"]] == ["rule_match", "value", "time_remaining"]


def test_strong_well_covered_match_is_still_pursued_automatically(db):
    policy = _enable(db)
    opp = opportunity(db, days=30, value=Decimal(10000000))
    match = _ranked(db, opp, semantic=0.95, wins=0.9)
    assert float(match.rank_score) >= 95
    result = _run()
    assert opp.id in result.pursued and _pursued(db, opp)
    db.expire_all()
    assert db.get(Match, match.id).status == "pursuing"
    assert not needs_eligibility_decision(match, opp, policy, datetime.now(UTC))


# ── the finding ──────────────────────────────────────────────────────────────

def test_value_and_deadline_alone_are_left_to_a_person(db):
    """The audit's probe: $100K, 21+ days, nothing else known scored 81.8 with 55% coverage."""
    policy = _enable(db)
    opp = opportunity(db, days=25, value=Decimal(100000))
    match = _ranked(db, opp)
    result = _run()
    assert opp.id not in result.pursued and not _pursued(db, opp)
    assert match.id in result.needs_eligibility_decision
    assert needs_eligibility_decision(match, opp, policy, datetime.now(UTC)), "the inbox flags it for a person"


def test_rule_match_points_do_not_lift_a_weak_match_over_the_minimum(db):
    """Well covered, but only 64 on the evidence; the rule_match points made it 78.7."""
    _enable(db)
    opp = opportunity(db, days=30, value=Decimal(100000))
    match = _ranked(db, opp, semantic=0.7)
    assert float(match.rank_score) == pytest.approx(78.7, abs=0.1)
    result = _run()
    assert opp.id not in result.pursued and not _pursued(db, opp)
    assert match.id not in result.needs_eligibility_decision
