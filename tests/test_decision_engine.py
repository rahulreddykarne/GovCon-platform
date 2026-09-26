"""Phase 8 tests: JEV decision bundles, hard rules, and persistence."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.decision.engine import (
    list_decision_runs,
    run_decision_bundle,
    run_preliminary_decision_package,
)
from govcon.decision.providers.rule_fallback import RuleDecisionProvider
from govcon.decision.schemas import ACCEPTANCE_BUNDLE_NAMES
from govcon.models import AIAnalysis, BidDecision, DecisionRun, Opportunity, Pursuit


def _create_opportunity(session: Session, **overrides) -> Opportunity:
    deadline = datetime.now(UTC) + timedelta(days=14)
    data = {
        "source": "sam",
        "source_id": f"phase8-{uuid4().hex}",
        "title": "Phase 8 decision test opportunity",
        "status": "open",
        "psc_code": "6515",
        "naics_code": "339112",
        "set_aside_code": "SBA",
        "response_deadline": deadline,
        "raw": {"fixture": True},
        "links": {},
    }
    data.update(overrides)
    row = Opportunity(**data)
    session.add(row)
    session.flush()
    return row


def _base_state(opportunity: Opportunity) -> dict:
    return {
        "opportunity": {
            "id": opportunity.id,
            "source": opportunity.source,
            "source_id": opportunity.source_id,
            "title": opportunity.title,
            "status": opportunity.status,
            "set_aside": opportunity.set_aside_code,
            "days_remaining": 10,
            "required_delivery_days": 12,
            "response_deadline": opportunity.response_deadline.isoformat() if opportunity.response_deadline else None,
        },
        "eligibility": {
            "sam_active": True,
            "set_aside_match": True,
            "mandatory_certifications_met": True,
        },
        "sourcing": {
            "product_found": True,
            "supplier_count": 3,
            "best_supplier_cost": 8500.0,
            "lead_time_days": 9,
        },
        "pricing": {
            "proposed_price": 10900.0,
            "supplier_cost": 8500.0,
            "margin_pct": 20.0,
            "historical_comparable_count": 8,
            "historical_median": 11200.0,
        },
        "compliance": {
            "mandatory_total": 12,
            "mandatory_missing": 0,
            "needs_review": 0,
            "critical_total": 3,
            "critical_unresolved": 0,
            "country_of_origin_conflict": False,
        },
        "analysis": {"missing_information": [], "risk_flags": []},
        "amendment": {"count": 0, "material": False},
        "review": {"review_policy": "conditional", "required_review_count": 1, "completed_review_count": 0},
        "scores": {"capability_fit_score": 0.82, "product_fit_score": 0.75},
        "signals": {"has_attachments": True, "requires_deep_analysis": True, "competitor_bucket_count": 1, "incumbent_signal": False},
        "outcome": {"evidence_count": 0, "no_bid_reason": None, "loss_reason": None},
        "source_snapshot_ids": [],
        "evidence_refs": [{"source": "fixture"}],
        "bundle_results": {},
    }


def test_acceptance_bundles_persist_and_validate(upgraded_engine) -> None:
    with Session(upgraded_engine) as session:
        opp = _create_opportunity(session)
        state = _base_state(opp)
        runs = []
        for bundle in ACCEPTANCE_BUNDLE_NAMES:
            execution = run_decision_bundle(
                session,
                opportunity_id=opp.id,
                bundle_name=bundle,
                state=state,
            )
            runs.append(execution)
            state["bundle_results"][bundle] = execution.result
        session.commit()

        stored = session.scalars(
            select(DecisionRun).where(DecisionRun.opportunity_id == opp.id).order_by(DecisionRun.id.asc())
        ).all()
        assert len(stored) == len(ACCEPTANCE_BUNDLE_NAMES)
        for row in stored:
            assert row.provider in {"rules", "jev", "llm"}
            assert row.bundle_version == "v1"
            assert row.decision_spec_name
            assert row.decision_spec_hash and len(row.decision_spec_hash) == 64
            assert row.result


def test_changed_material_state_creates_fresh_run(upgraded_engine) -> None:
    with Session(upgraded_engine) as session:
        opp = _create_opportunity(session)
        state = _base_state(opp)
        first = run_decision_bundle(
            session,
            opportunity_id=opp.id,
            bundle_name="bid_decision",
            state=state,
        )
        state["pricing"]["margin_pct"] = 3.0
        state["pricing"]["historical_comparable_count"] = 1
        second = run_decision_bundle(
            session,
            opportunity_id=opp.id,
            bundle_name="bid_decision",
            state=state,
        )
        session.commit()

        assert first.run.id != second.run.id
        assert second.run.supersedes_run_id == first.run.id
        assert second.run.input_state_hash != first.run.input_state_hash


def test_hard_rule_override_beats_bid_signal(upgraded_engine) -> None:
    with Session(upgraded_engine) as session:
        opp = _create_opportunity(session, status="closed")
        state = _base_state(opp)
        state["opportunity"]["status"] = "closed"
        execution = run_decision_bundle(
            session,
            opportunity_id=opp.id,
            bundle_name="bid_decision",
            state=state,
        )
        session.commit()

        assert execution.result["recommendation"] == "no_bid"
        assert execution.result["human_review_required"] is True
        assert execution.result["hard_rule_blockers"]


def test_low_confidence_routes_to_review(upgraded_engine, monkeypatch: pytest.MonkeyPatch) -> None:
    from govcon.decision.provider import ProviderDecision

    def _forced_low_confidence(self, *, bundle_name, bundle_version, state):  # noqa: ANN001
        return ProviderDecision(
            provider="rules",
            model="deterministic-v1",
            confidence=0.2,
            result={
                "recommendation": "bid",
                "opportunity_strength": "high",
                "evidence_confidence": "high",
                "sufficient_information": True,
                "unresolved_no_bid_issue": False,
                "execution_risk": "medium",
                "commercial_attractiveness": "high",
                "compliance_risk": "low",
                "human_review_required": False,
                "recommend_bid_approval": True,
                "hard_rule_blockers": [],
            },
        )

    monkeypatch.setattr(RuleDecisionProvider, "decide", _forced_low_confidence)

    with Session(upgraded_engine) as session:
        opp = _create_opportunity(session)
        execution = run_decision_bundle(
            session,
            opportunity_id=opp.id,
            bundle_name="bid_decision",
            state=_base_state(opp),
        )
        session.commit()

        assert execution.result["recommendation"] == "review"
        assert execution.result["human_review_required"] is True
        assert execution.result["recommend_bid_approval"] is False


def test_submission_ready_cannot_set_submitted_stage(upgraded_engine, monkeypatch: pytest.MonkeyPatch) -> None:
    from govcon.decision.provider import ProviderDecision

    original_decide = RuleDecisionProvider.decide

    def _ready_submission(self, *, bundle_name, bundle_version, state):  # noqa: ANN001
        if bundle_name == "submission_readiness":
            return ProviderDecision(
                provider="rules",
                model="deterministic-v1",
                confidence=0.95,
                result={
                    "status": "ready",
                    "blocking_issue_exists": False,
                    "document_package_complete": "yes",
                    "override_candidate": "no",
                    "deadline_critical": False,
                    "another_review_required": False,
                    "human_verification_required": True,
                    "immediate_alert_required": False,
                },
            )
        return original_decide(self, bundle_name=bundle_name, bundle_version=bundle_version, state=state)

    monkeypatch.setattr(RuleDecisionProvider, "decide", _ready_submission)

    with Session(upgraded_engine) as session:
        opp = _create_opportunity(session)
        pursuit = Pursuit(opportunity_id=opp.id, stage="evaluating", sourcing_cost=8000, quote_price=10000)
        session.add(pursuit)
        session.flush()
        execution = run_decision_bundle(
            session,
            opportunity_id=opp.id,
            bundle_name="submission_readiness",
            state=_base_state(opp),
        )
        session.commit()
        session.refresh(pursuit)

        assert execution.result["status"] == "ready"
        assert pursuit.stage == "evaluating"


def test_preliminary_package_persists_multiple_artifacts(upgraded_engine) -> None:
    with Session(upgraded_engine) as session:
        opp = _create_opportunity(session)
        session.add(Pursuit(opportunity_id=opp.id, stage="evaluating", sourcing_cost=8600, quote_price=11000))
        session.flush()
        package = run_preliminary_decision_package(session, opportunity_id=opp.id)
        session.commit()

        assert package.analysis.id is not None
        assert package.bid_decision.id is not None
        assert package.package_output["human_decision_authority"] == "required"
        assert package.package_output["guardrails"]["bid_approval_requires_human_decision"] is True

        decision_runs = list_decision_runs(session, opportunity_id=opp.id, limit=20)
        assert len(decision_runs) >= 8
        persisted_analysis = session.get(AIAnalysis, package.analysis.id)
        assert persisted_analysis is not None
        assert persisted_analysis.analysis_type == "decision_package"
        persisted_bid = session.get(BidDecision, package.bid_decision.id)
        assert persisted_bid is not None
        assert persisted_bid.human_decision is None


def test_jev_unavailable_falls_back_to_rules(upgraded_engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    monkeypatch.setenv("JEV_ENABLED", "true")
    with Session(upgraded_engine) as session:
        opp = _create_opportunity(session)
        execution = run_decision_bundle(
            session,
            opportunity_id=opp.id,
            bundle_name="opportunity_triage",
            state=_base_state(opp),
        )
        session.commit()
        assert execution.provider == "rules"


def test_decision_fixture_contract_and_boundaries() -> None:
    fixture_dir = Path(__file__).parent / "fixtures" / "decisions"
    cases = sorted(fixture_dir.glob("*.json"))
    assert len(cases) == 13

    provider = RuleDecisionProvider()
    for case in cases:
        payload = json.loads(case.read_text(encoding="utf-8"))
        assert "state" in payload
        assert "expected_allowed_decisions" in payload
        assert "must_escalate" in payload
        decision = provider.decide(
            bundle_name="bid_decision",
            bundle_version="v1",
            state=payload["state"],
        )
        recommendation = decision.result["recommendation"]
        assert recommendation in payload["expected_allowed_decisions"], case.name
        if payload["must_escalate"]:
            assert decision.result["human_review_required"] is True, case.name


def test_jev_spec_files_have_required_contract_fields() -> None:
    spec_dir = Path(__file__).parent.parent / "src" / "govcon" / "prompts" / "jev"
    spec_files = sorted(spec_dir.glob("*_v1.yaml"))
    assert len(spec_files) == 13
    required_tokens = [
        "name:",
        "version:",
        "provider:",
        "input_schema:",
        "objective:",
        "questions:",
        "policy:",
        "unknown_is_not_negative:",
        "low_confidence_escalate_to_review:",
        "consequential_action_requires_human:",
    ]
    for path in spec_files:
        text = path.read_text(encoding="utf-8")
        for token in required_tokens:
            assert token in text, f"{path.name} missing {token}"
