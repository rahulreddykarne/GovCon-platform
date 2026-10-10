"""Bid/no-bid scorecard: hard blockers, missing inputs, and rules-vs-model disagreement."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from test_web_ui import _make_user

from govcon.decision.engine import (
    build_decision_state,
    run_decision_bundle,
    run_preliminary_decision_package,
)
from govcon.decision.provider import ProviderDecision
from govcon.decision.rules import (
    arbitrate_bid,
    coverage_adjusted_confidence,
    evaluate_hard_rules,
)
from govcon.decision.scorecard import build_scorecard
from govcon.models import Opportunity, Requirement


def _opp(db, **values) -> Opportunity:
    data = {"source": "sam", "source_id": uuid4().hex, "title": f"Scorecard {uuid4().hex[:6]}",
            "status": "open", "response_deadline": datetime.now(UTC) + timedelta(days=20),
            "raw": {}, "links": {}}
    data.update(values)
    opp = Opportunity(**data)
    db.add(opp)
    db.flush()
    return opp


def _bid_result(**overrides) -> dict:
    data = {
        "recommendation": "bid", "opportunity_strength": "high", "evidence_confidence": "high",
        "sufficient_information": True, "unresolved_no_bid_issue": False, "execution_risk": "low",
        "commercial_attractiveness": "high", "compliance_risk": "low", "human_review_required": False,
        "recommend_bid_approval": True, "hard_rule_blockers": [],
    }
    data.update(overrides)
    return data


def test_awarded_notice_is_a_hard_no_bid():
    findings = evaluate_hard_rules({"opportunity": {"status": "awarded"}, "eligibility": {},
                                    "sourcing": {}, "compliance": {}})
    assert any(f.code == "notice_not_open" and f.effect == "no_bid" for f in findings)
    from govcon.decision.rules import apply_hard_rule_override
    out = apply_hard_rule_override("bid_decision", _bid_result(), findings)
    assert out["recommendation"] == "no_bid" and out["recommend_bid_approval"] is False


def test_unknown_set_aside_forces_needs_info():
    findings = evaluate_hard_rules({
        "opportunity": {"status": "open", "set_aside": "SBA"},
        "eligibility": {"set_aside_match": None, "sam_active": True},
        "sourcing": {}, "compliance": {},
    })
    assert any(f.code == "set_aside_unverified" and f.effect == "needs_info" for f in findings)
    from govcon.decision.rules import apply_hard_rule_override
    out = apply_hard_rule_override("bid_decision", _bid_result(), findings)
    assert out["recommendation"] == "insufficient_information"
    assert out["needs_information"]


def test_unmet_mandatory_requirement_forces_no_bid(db):
    opp = _opp(db)
    db.add(Requirement(opportunity_id=opp.id, requirement_text="Must be registered in SAM",
                       mandatory=True, status="missing", severity="critical"))
    db.flush()
    state = build_decision_state(db, opp.id)
    assert state["compliance"]["mandatory_unmet"] == 1
    execution = run_decision_bundle(db, opportunity_id=opp.id, bundle_name="bid_decision", state=state)
    assert execution.result["recommendation"] == "no_bid"
    assert any("not met" in b for b in execution.result["hard_rule_blockers"])
    db.rollback()


def test_unresolved_mandatory_without_an_unmet_row_is_needs_info(db):
    opp = _opp(db)
    db.add(Requirement(opportunity_id=opp.id, requirement_text="Unknown cert",
                       mandatory=True, status="unknown", severity="high"))
    db.flush()
    execution = run_decision_bundle(db, opportunity_id=opp.id, bundle_name="bid_decision")
    assert execution.result["recommendation"] == "insufficient_information"
    assert execution.result["hard_rule_blockers"] == []
    assert any("unresolved" in item for item in execution.result["needs_information"])
    db.rollback()


def test_missing_inputs_drop_confidence():
    assert coverage_adjusted_confidence(0.8, 1.0) == 0.8
    assert coverage_adjusted_confidence(0.8, 0.0) == 0.4
    half = coverage_adjusted_confidence(0.8, 0.5)
    assert half is not None and 0.4 < half < 0.8


def test_scorecard_lists_each_factor_and_labels_a_market_estimate():
    card = build_scorecard({
        "opportunity": {"status": "open", "set_aside": "SBA", "days_remaining": 14,
                        "estimated_value_min": 10000, "estimated_value_max": 20000},
        "eligibility": {"set_aside_match": True, "sam_active": True, "mandatory_certifications_met": True},
        "provenance": {"set_aside_match": {"detail": "company facts assert small_business"},
                       "sam_active": {"detail": "SAM registration active"},
                       "capability_fit_score": {"detail": "matches 1 enabled watchlist"}},
        "scores": {"capability_fit_score": 0.7},
        "company_inputs": {"past_performance": ["DLA gloves 2024"]},
        "pricing": {"margin_pct": 18, "cost_basis": "web_estimate", "cost_basis_detail": "web market price estimate",
                    "historical_median_unit_price": 12.5, "historical_comparable_count": 4},
        "signals": {"distinct_awardee_count": 3, "repeat_awardee_signal": False},
        "compliance": {"mandatory_total": 4, "mandatory_unmet": 0, "mandatory_missing": 0, "critical_unresolved": 0},
        "sourcing": {},
        "analysis": {"risk_flags": []},
        "amendment": {"material": False},
    })
    keys = [f.key for f in card.factors]
    assert keys == ["eligibility", "past_performance", "capability", "price_margin",
                    "competition", "compliance", "schedule", "risk"]
    price = next(f for f in card.factors if f.key == "price_margin")
    assert price.known and any("estimate" in item.lower() for item in price.evidence)
    assert card.estimated_value_note and "not revenue" in card.estimated_value_note
    assert "revenue" not in (card.estimated_value_note or "").replace("not revenue", "")
    empty = build_scorecard({"opportunity": {}, "eligibility": {}, "sourcing": {}, "pricing": {},
                             "compliance": {}, "signals": {}, "scores": {}, "company_inputs": {},
                             "provenance": {}, "analysis": {}, "amendment": {}})
    assert empty.coverage < 1
    assert any("past performance" in item for item in empty.missing_inputs)


def test_disagreement_sends_the_bid_to_review():
    agreed, record = arbitrate_bid(_bid_result(), 0.8, model_provider="jev",
                                   model_result=_bid_result(), model_confidence=0.9)
    assert agreed["recommendation"] == "bid" and record["winner"] == "agree"
    reviewed, record = arbitrate_bid(_bid_result(), 0.8, model_provider="jev",
                                     model_result=_bid_result(recommendation="no_bid"), model_confidence=0.9)
    assert reviewed["recommendation"] == "review" and record["winner"] == "human_review"
    assert reviewed["recommend_bid_approval"] is False
    needs, record = arbitrate_bid(_bid_result(recommendation="insufficient_information"), 0.5,
                                  model_provider="jev", model_result=_bid_result(), model_confidence=0.9)
    assert needs["recommendation"] == "insufficient_information" and record["winner"] == "rules"


def test_engine_keeps_the_rules_result_when_the_model_disagrees(db, monkeypatch):
    class FakeJev:
        name = "jev"
        def decide(self, *, bundle_name, bundle_version, state):
            return ProviderDecision(provider="jev", model="synthetic", confidence=0.92,
                                    result=_bid_result(recommendation="no_bid"))

    monkeypatch.setattr("govcon.decision.engine.JevDecisionProvider.from_settings", classmethod(lambda cls, *a, **k: FakeJev()))
    monkeypatch.setenv("DECISION_PRIMARY_PROVIDER", "jev")
    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setenv("JEV_API_KEY", "synthetic-not-used")
    from govcon.config import get_settings
    get_settings.cache_clear()
    opp = _opp(db)
    state = {
        "opportunity": {"id": opp.id, "status": "open", "days_remaining": 20, "set_aside": None},
        "eligibility": {"set_aside_match": True, "sam_active": True, "mandatory_certifications_met": True},
        "sourcing": {"product_found": True, "lead_time_days": 5},
        "pricing": {"margin_pct": 22, "historical_comparable_count": 8, "cost_basis": "supplier_quote"},
        "compliance": {"mandatory_total": 1, "mandatory_unmet": 0, "mandatory_missing": 0, "critical_unresolved": 0},
        "analysis": {"missing_information": []},
        "scores": {"capability_fit_score": 0.8},
        "signals": {"distinct_awardee_count": 2, "repeat_awardee_signal": False, "has_attachments": False},
        "company_inputs": {"past_performance": ["prior DLA award"]},
        "provenance": {},
        "bundle_results": {
            "market_and_pricing": {"margin_quality": "strong", "commercial_attractiveness": "high",
                                   "historical_comparability": "high", "competition_level": "medium"},
            "eligibility_and_execution": {"stop_evaluation": False, "capability_match": "high",
                                          "execution_risk": "low", "delivery_feasibility": "yes"},
        },
        "review": {"review_policy": "conditional", "required_review_count": 1, "completed_review_count": 0},
        "amendment": {"count": 0, "material": False, "material_known": True},
        "source_snapshot_ids": [],
        "evidence_refs": [],
    }
    execution = run_decision_bundle(db, opportunity_id=opp.id, bundle_name="bid_decision", state=state)
    assert execution.result["recommendation"] == "review"
    assert execution.result["arbitration"]["winner"] == "human_review"
    get_settings.cache_clear()
    db.rollback()


def test_package_stores_the_scorecard_and_never_calls_a_bid_an_approval(db):
    opp = _opp(db, set_aside_code=None, estimated_value_min=5000, estimated_value_max=9000)
    package = run_preliminary_decision_package(db, opportunity_id=opp.id)
    assert package.package_output["human_decision_authority"] == "required"
    assert package.package_output["guardrails"]["bid_approval_requires_human_decision"] is True
    note = package.package_output["estimated_value_note"]
    assert note and "not revenue" in note
    assert package.bid_decision.rules_result["scorecard"]["factors"]
    assert package.bid_decision.recommendation in {"bid", "no_bid", "review", "insufficient_information"}
    db.rollback()


def test_decision_page_renders_the_scorecard(db, client):
    user, token = _make_user(db, f"score-{uuid4().hex}@example.test", "reviewer")
    client.cookies.set("govcon_session", token)
    opp = _opp(db, set_aside_code="SBA", estimated_value_min=1000, estimated_value_max=2000)
    db.add(Requirement(opportunity_id=opp.id, requirement_text="Small business set-aside",
                       mandatory=True, status="unknown", requirement_type="set_aside"))
    db.commit()
    page = client.get(f"/workspace/{opp.id}?tab=review")
    assert page.status_code == 200
    text = page.text
    assert "Scorecard" in text
    assert "Eligibility / set-aside" in text and "Price / margin" in text
    assert "Human approval required" in text
    assert "not revenue" in text
    assert "never submits" in text
