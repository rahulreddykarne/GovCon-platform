"""H9: decision inputs come from evidence, are three-state, and carry provenance."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from govcon.ai.analysis_types import AnalysisType
from govcon.decision.engine import build_decision_state, run_decision_bundle
from govcon.decision.providers.rule_fallback import RuleDecisionProvider
from govcon.models import AIAnalysis, Award, Match, Opportunity, Pursuit, Watchlist


@pytest.fixture()
def session(upgraded_engine):
    with Session(upgraded_engine) as s:
        yield s
        s.rollback()


@pytest.fixture()
def company(tmp_path: Path, monkeypatch):
    """Write a company-facts profile and point COMPANY_FACTS_PATH at it."""
    from govcon.config import get_settings

    def write(facts: dict) -> None:
        path = tmp_path / "company_facts.json"
        path.write_text(json.dumps(facts), encoding="utf-8")
        monkeypatch.setenv("COMPANY_FACTS_PATH", str(path))
        get_settings.cache_clear()

    write({})
    yield write
    get_settings.cache_clear()


def _opp(session: Session, **fields) -> Opportunity:
    values = {
        "source": "sam",
        "source_id": f"sig-{secrets.token_hex(6)}",
        "title": "Signal fixture",
        "status": "open",
        "psc_code": f"Z{secrets.randbelow(900) + 100}",
        "naics_code": "339112",
        "response_deadline": datetime.now(UTC) + timedelta(days=20),
        "raw": {},
        "links": {},
    }
    values.update(fields)
    opp = Opportunity(**values)
    session.add(opp)
    session.flush()
    return opp


def _summary(session: Session, opp: Opportunity, **output) -> None:
    session.add(
        AIAnalysis(
            opportunity_id=opp.id,
            analysis_type=AnalysisType.SOLICITATION_SUMMARY,
            schema_version="solicitation_analysis.v1",
            output_json={"summary": "fixture", **output},
        )
    )
    session.flush()


def test_set_aside_uses_the_company_profile(session, company) -> None:
    company({"socioeconomic": {"small_business": True, "8a": False}})
    assert build_decision_state(session, _opp(session, set_aside_code="SBA").id)["eligibility"]["set_aside_match"] is True
    assert build_decision_state(session, _opp(session, set_aside_code="8A").id)["eligibility"]["set_aside_match"] is False
    assert build_decision_state(session, _opp(session, set_aside_code="HZC").id)["eligibility"]["set_aside_match"] is None
    assert build_decision_state(session, _opp(session, set_aside_code=None).id)["eligibility"]["set_aside_match"] is True
    assert build_decision_state(session, _opp(session, set_aside_code="N").id)["eligibility"]["set_aside_match"] is True
    # DIBBS 'A' (8(a)) is mapped too.
    assert build_decision_state(session, _opp(session, source="dibbs", set_aside_code="A").id)["eligibility"]["set_aside_match"] is False


def test_ineligible_set_aside_blocks_the_bid(session, company) -> None:
    company({"socioeconomic": {"8a": False}})
    opp = _opp(session, set_aside_code="8A")
    execution = run_decision_bundle(session, opportunity_id=opp.id, bundle_name="bid_decision")
    assert execution.result["recommendation"] == "no_bid"
    assert any("set-aside" in b.lower() for b in execution.result["hard_rule_blockers"])


def test_listed_certifications_are_checked_against_held_ones(session, company) -> None:
    opp = _opp(session)
    _summary(session, opp, certifications=["ISO 9001"])
    company({"certifications": ["ISO 9001:2015"]})
    assert build_decision_state(session, opp.id)["eligibility"]["mandatory_certifications_met"] is True
    company({"certifications": ["AS9100"]})
    assert build_decision_state(session, opp.id)["eligibility"]["mandatory_certifications_met"] is False
    company({})
    state = build_decision_state(session, opp.id)
    assert state["eligibility"]["mandatory_certifications_met"] is None
    assert any("mandatory_certifications_met is unknown" in m for m in state["analysis"]["missing_information"])


def test_line_items_do_not_mean_sourced_and_missing_analysis_does_not_block(session, company) -> None:
    opp = _opp(session)
    _summary(session, opp, items=[{"description": "gloves"}])
    state = build_decision_state(session, opp.id)
    assert state["sourcing"]["product_found"] is None
    execution = run_decision_bundle(session, opportunity_id=opp.id, bundle_name="bid_decision")
    assert not any("cannot currently be sourced" in b for b in execution.result.get("hard_rule_blockers", []))

    session.add(Pursuit(opportunity_id=opp.id, stage="sourcing", supplier="Acme Gloves", sourcing_cost=Decimal("100")))
    session.flush()
    state = build_decision_state(session, opp.id)
    assert state["sourcing"]["product_found"] is True
    assert state["provenance"]["product_found"]["source"].startswith("pursuit")


def test_sam_registration_from_company_facts(session, company) -> None:
    opp = _opp(session)
    company({"sam_registration_status": "Active", "sam_expiration_date": (datetime.now(UTC) + timedelta(days=365)).date().isoformat()})
    assert build_decision_state(session, opp.id)["eligibility"]["sam_active"] is True
    company({"sam_registration_status": "Inactive"})
    assert build_decision_state(session, opp.id)["eligibility"]["sam_active"] is False
    company({})
    assert build_decision_state(session, opp.id)["eligibility"]["sam_active"] is None


def test_capability_comes_from_watchlists_and_history(session, company) -> None:
    opp = _opp(session)
    state = build_decision_state(session, opp.id)
    assert state["scores"]["capability_fit_score"] is None
    watchlist = Watchlist(name=f"sig-{secrets.token_hex(4)}", enabled=True, psc_codes=[opp.psc_code])
    session.add(watchlist)
    session.flush()
    session.add(Match(opportunity_id=opp.id, watchlist_id=watchlist.id, status="new"))
    session.flush()
    company({"naics_codes": ["339"]})
    state = build_decision_state(session, opp.id)
    assert state["scores"]["capability_fit_score"] >= 0.75
    assert "watchlist" in state["provenance"]["capability_fit_score"]["detail"]


def test_prices_compare_per_unit_against_unit_priced_awards_only(session, company) -> None:
    opp = _opp(session, nsn=f"6515-01-{secrets.randbelow(900) + 100}-{secrets.randbelow(9000) + 1000}", quantity=Decimal("100"))
    for unit, total in ((Decimal("2.00"), Decimal("200")), (Decimal("2.20"), Decimal("220")), (None, Decimal("5000000"))):
        session.add(
            Award(
                source="usaspending",
                award_id=f"sig-award-{secrets.token_hex(6)}",
                nsn=opp.nsn,
                psc_code=opp.psc_code,
                recipient_name="Same Vendor",
                unit_price=unit,
                total_obligation=total,
                raw={},
            )
        )
    session.add(Pursuit(opportunity_id=opp.id, stage="sourcing", quote_price=Decimal("190"), sourcing_cost=Decimal("150")))
    session.flush()
    state = build_decision_state(session, opp.id)
    pricing = state["pricing"]
    assert pricing["historical_comparable_count"] == 2
    assert pricing["historical_median_unit_price"] == pytest.approx(2.10)
    assert pricing["proposed_unit_price"] == pytest.approx(1.90)
    assert "historical_median" not in pricing  # contract totals are not a price benchmark
    market, _ = RuleDecisionProvider()._bundle_market_and_pricing(state)
    assert market["supplier_cost_competitiveness"] == "yes"
    assert state["signals"]["repeat_awardee_signal"] is True


def test_amendment_count_alone_is_not_material(session, company) -> None:
    opp = _opp(session)
    _summary(session, opp, amendment_status={"amendment_count": 2})
    state = build_decision_state(session, opp.id)
    assert state["amendment"]["count"] == 2
    assert state["amendment"]["material"] is False and state["amendment"]["material_known"] is False
    bundle, _ = RuleDecisionProvider()._bundle_compliance_and_amendment(state)
    assert bundle["amendment_material"] is False


def test_every_factor_has_provenance(session, company) -> None:
    state = build_decision_state(session, _opp(session).id)
    for factor in ("set_aside_match", "sam_active", "mandatory_certifications_met", "product_found",
                   "capability_fit_score", "historical_median_unit_price", "repeat_awardee_signal", "amendment_material"):
        assert {"source", "confidence"} <= set(state["provenance"][factor])
