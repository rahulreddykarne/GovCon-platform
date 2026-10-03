"""H7: analysis types are shared, producers exist, and the UI shows the results."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from web_client import CsrfTestClient as TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.ai.analysis_types import AnalysisType
from govcon.ai.providers.deepseek import DeepSeekResult
from govcon.ai.structured import StructuredCallError
from govcon.intelligence.ai_analyses import (
    AnalysisInputMissing,
    run_market_analysis,
    run_pricing_analysis,
    run_supplier_analysis,
)
from govcon.models import AIAnalysis, Award, Opportunity, Pursuit, User
from govcon.workflow.source_revision import SOURCE_REVISION_KEY

MARKET = {
    "historical_winners": ["Glove Supply Co"],
    "recurring_vendors": ["Glove Supply Co"],
    "incumbent_signals": [],
    "price_comparability": "two awards with stated unit prices",
    "agency_buying_patterns": "DLA buys quarterly",
    "competition_signals": ["3 distinct awardees"],
    "comparable_awards": [],
}
SUPPLIER = {"candidates": [{"supplier": "Acme Gloves", "product": "Nitrile glove", "delivery_lead_time_risk": "medium"}], "overall_sourcing_risk": "medium", "recommended_next_steps": ["Get a written quote"]}
PRICING = {"historical_comparability": "moderate", "proposed_price_position": "below median unit price", "expected_margin_quality": "good", "confidence": "medium", "missing_cost_inputs": ["freight"]}


class FakeProvider:
    name = "deepseek"

    def complete(self, *, purpose: str = "", model=None, **_kwargs):
        payload = {"market_analysis": MARKET, "supplier_analysis": SUPPLIER, "pricing_analysis": PRICING}[purpose]
        return DeepSeekResult(content=json.dumps(payload), model=model or "deepseek-flash", usage={}, latency_ms=1)


@pytest.fixture()
def session(upgraded_engine):
    with Session(upgraded_engine) as s:
        yield s
        s.rollback()


def _opp(session: Session) -> Opportunity:
    opp = Opportunity(
        source="sam",
        source_id=f"intel-{secrets.token_hex(6)}",
        title="Nitrile gloves",
        status="open",
        psc_code="6515",
        nsn="6515-01-519-8818",
        quantity=Decimal("100"),
        unit="PR",
        response_deadline=datetime.now(UTC) + timedelta(days=10),
        raw={},
        links={},
    )
    session.add(opp)
    session.flush()
    session.add(
        Award(
            source="usaspending",
            award_id=f"intel-award-{secrets.token_hex(6)}",
            nsn=opp.nsn,
            psc_code="6515",
            recipient_name="Glove Supply Co",
            unit_price=Decimal("2.10"),
            quantity=Decimal("500"),
            total_obligation=Decimal("1050"),
            raw={},
        )
    )
    session.flush()
    return opp


def test_analysis_type_values_are_stable() -> None:
    assert AnalysisType.SOLICITATION_SUMMARY == "solicitation_summary"
    assert AnalysisType.SOURCING == "sourcing_analysis"


def test_market_analysis_runs_under_the_default_policy(session) -> None:
    opp = _opp(session)
    with patch("govcon.ai.structured.get_provider", return_value=FakeProvider()):
        analysis = run_market_analysis(session, opp.id)
    assert analysis.analysis_type == AnalysisType.MARKET
    assert analysis.output_json["historical_winners"] == ["Glove Supply Co"]
    assert analysis.context_manifest[SOURCE_REVISION_KEY]
    assert analysis.generation_settings["classification"] == "PUBLIC"


def test_supplier_and_pricing_need_recorded_inputs(session) -> None:
    opp = _opp(session)
    with pytest.raises(AnalysisInputMissing):
        run_supplier_analysis(session, opp.id)
    with pytest.raises(AnalysisInputMissing):
        run_pricing_analysis(session, opp.id)


def test_supplier_and_pricing_are_proprietary_and_blocked_by_default(session) -> None:
    opp = _opp(session)
    session.add(Pursuit(opportunity_id=opp.id, stage="sourcing", supplier="Acme Gloves", sourcing_cost=Decimal("150"), quote_price=Decimal("200")))
    session.flush()
    with patch("govcon.ai.structured.get_provider", return_value=FakeProvider()):
        for producer in (run_supplier_analysis, run_pricing_analysis):
            with pytest.raises(StructuredCallError) as exc:
                producer(session, opp.id)
            assert exc.value.reason == "blocked_by_policy"


@pytest.mark.usefixtures("allow_proprietary_ai")
def test_supplier_and_pricing_produce_typed_analyses(session) -> None:
    opp = _opp(session)
    session.add(Pursuit(opportunity_id=opp.id, stage="sourcing", supplier="Acme Gloves", sourcing_cost=Decimal("150"), quote_price=Decimal("200")))
    session.flush()
    with patch("govcon.ai.structured.get_provider", return_value=FakeProvider()):
        supplier = run_supplier_analysis(session, opp.id)
        pricing = run_pricing_analysis(session, opp.id)
    assert supplier.analysis_type == AnalysisType.SOURCING
    assert pricing.analysis_type == AnalysisType.PRICING
    assert supplier.generation_settings["classification"] == "PROPRIETARY"
    assert pricing.output_json["proposed_price_position"] == "below median unit price"


# ── web ──────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def client(upgraded_engine):
    from govcon.web.app import create_app

    with TestClient(create_app(), follow_redirects=False) as c:
        yield c


def _token(session: Session, role: str) -> str:
    from govcon.collaboration.users import create_session, hash_password

    user = User(
        email=f"intel-{role}-{secrets.token_hex(4)}@example.com",
        display_name=f"intel {role}",
        password_hash=hash_password("TestPassword123!"),
        role=role,
        is_active=True,
    )
    session.add(user)
    session.flush()
    raw = create_session(session, user)
    session.commit()
    return raw


def test_ui_shows_the_solicitation_summary(client, session) -> None:
    opp = _opp(session)
    marker = f"Summary marker {secrets.token_hex(4)}"
    session.add(
        AIAnalysis(
            opportunity_id=opp.id,
            analysis_type=AnalysisType.SOLICITATION_SUMMARY,
            schema_version="solicitation_analysis.v1",
            output_json={"summary": marker},
        )
    )
    session.commit()
    token = _token(session, "read_only")
    assert marker in client.get(f"/opp/{opp.id}", cookies={"govcon_session": token}).text
    assert marker in client.get(f"/workspace/{opp.id}?tab=overview", cookies={"govcon_session": token}).text


def test_market_tab_runs_and_renders_the_analysis(client, session) -> None:
    opp = _opp(session)
    session.commit()
    token = _token(session, "reviewer")
    with patch("govcon.ai.structured.get_provider", return_value=FakeProvider()):
        resp = client.post(f"/workspace/{opp.id}/analyze/market", cookies={"govcon_session": token})
    assert resp.status_code == 303 and "notice=" in resp.headers["location"]
    page = client.get(f"/workspace/{opp.id}?tab=market", cookies={"govcon_session": token}).text
    assert "DLA buys quarterly" in page and "Glove Supply Co" in page

    viewer = _token(session, "read_only")
    denied = client.post(f"/workspace/{opp.id}/analyze/market", cookies={"govcon_session": viewer})
    assert "error=" in denied.headers["location"]


def test_pricing_tab_reports_a_policy_block_to_the_user(client, session) -> None:
    opp = _opp(session)
    session.add(Pursuit(opportunity_id=opp.id, stage="sourcing", quote_price=Decimal("200"), sourcing_cost=Decimal("150")))
    session.commit()
    token = _token(session, "reviewer")
    resp = client.post(f"/workspace/{opp.id}/analyze/pricing", cookies={"govcon_session": token})
    assert "error=" in resp.headers["location"] and "blocked_by_policy" in resp.headers["location"]
    assert session.scalar(
        select(AIAnalysis).where(AIAnalysis.opportunity_id == opp.id, AIAnalysis.analysis_type == AnalysisType.PRICING)
    ) is None


def test_every_tab_renders_with_award_data(client, session) -> None:
    """Currency columns used an invalid %-format and crashed once awards existed."""
    from govcon.models import Vendor

    opp = _opp(session)
    uei = f"UEI{secrets.token_hex(4).upper()}"
    session.add(Vendor(uei=uei, legal_name="Glove Supply Co"))
    session.add(
        Award(
            source="usaspending",
            award_id=f"intel-award-{secrets.token_hex(6)}",
            nsn=opp.nsn,
            psc_code="6515",
            recipient_uei=uei,
            recipient_name="Glove Supply Co",
            unit_price=Decimal("2.1234"),
            total_obligation=Decimal("1234567.89"),
            raw={},
        )
    )
    session.commit()
    token = _token(session, "read_only")
    for tab in ("overview", "ai_decision", "requirements", "market", "awards", "products", "pricing",
                "competitors", "compliance", "review", "proposal", "submission", "activity"):
        resp = client.get(f"/workspace/{opp.id}?tab={tab}", cookies={"govcon_session": token})
        assert resp.status_code == 200, tab
    awards = client.get(f"/workspace/{opp.id}?tab=awards", cookies={"govcon_session": token}).text
    assert "$1,234,567.89" in awards
    vendor = client.get(f"/vendors?q={uei}", cookies={"govcon_session": token})
    assert vendor.status_code == 200 and "$1,234,568" in vendor.text
