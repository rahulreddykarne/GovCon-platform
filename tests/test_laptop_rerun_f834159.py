"""Laptop rerun at f834159: part commit, adaptive chunks, analyze, budget, UI."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_web_ui import _make_user
from web_client import CsrfTestClient

from govcon.config import Settings
from govcon.models import (
    AIAnalysis,
    AIProviderCall,
    AuditEvent,
    BidDecision,
    ComplianceRun,
    FilePage,
    Opportunity,
    Pursuit,
    Requirement,
    StoredFile,
    Task,
)
from govcon.web.app import create_app


@pytest.fixture()
def db(upgraded_engine):
    with Session(upgraded_engine) as session:
        yield session


@pytest.fixture()
def client(upgraded_engine):
    with CsrfTestClient(create_app(), follow_redirects=False) as client:
        yield client


def _opp(db, **values) -> Opportunity:
    data = {
        "source": "dibbs",
        "source_id": f"f834159-{uuid4().hex}",
        "title": f"DIBBS RFQ {uuid4().hex[:8]}",
        "status": "open",
        "raw": {},
        "links": {},
    }
    data.update(values)
    row = Opportunity(**data)
    db.add(row)
    db.flush()
    return row


def test_part_commit_survives_caller_rollback_and_ledger_fk(db, monkeypatch) -> None:
    from govcon.ai.structured import run_structured_prompt
    from govcon.ai.usage_log import attach_call_ids, record_call
    from govcon.prompting.registry import sync_prompts
    from govcon.security.classification import DataClassification

    settings = Settings(_env_file=None, ai_proposal_budget_share=0)
    sync_prompts(db, Path(__file__).parent.parent / "src" / "govcon" / "prompts", settings=settings)
    opp = _opp(db)
    db.commit()

    class Fake:
        name = "deepseek"

        def complete(self, **kwargs):
            return SimpleNamespace(
                content=(
                    '{"summary":"This committed analysis part has enough substance to reuse.",'
                    '"source_refs":[{"quote":"See page 1 of the RFQ document."}]}'
                ),
                usage={"prompt_tokens": 40, "completion_tokens": 12},
                model="synthetic-model", provider="deepseek", latency_ms=1, finish_reason="stop",
            )

    monkeypatch.setattr("govcon.ai.structured.get_provider", lambda *a, **k: Fake())
    result = run_structured_prompt(
        db, opportunity_id=opp.id, prompt_name="solicitation_analysis",
        analysis_type="solicitation_summary",
        variables={"OPPORTUNITY_JSON": {}, "SOURCE_PACKAGE_JSON": "page one of the rfq"},
        context_manifest={"sha": "part-commit", "part": 1}, settings=settings,
        classification=DataClassification.PUBLIC,
    )
    analysis_id = result.analysis.id
    assert analysis_id is not None
    assert (result.analysis.generation_settings or {}).get("role") == "part"

    db.add(BidDecision(opportunity_id=opp.id, recommendation="review"))
    db.rollback()
    db.expire_all()
    stored = db.get(AIAnalysis, analysis_id)
    assert stored is not None
    assert stored.output_json["summary"].startswith("This committed analysis part")

    orphan = record_call(
        db, provider="deepseek", purpose="solicitation_analysis", status="succeeded",
        model="synthetic-model", opportunity_id=opp.id,
        usage={"prompt_tokens": 1, "completion_tokens": 1},
    )
    attach_call_ids(db, [orphan], analysis_id=10_000_000)
    db.expire_all()
    row = db.get(AIProviderCall, orphan)
    assert row is not None
    assert row.analysis_id is None

    attach_call_ids(db, [orphan], analysis_id=analysis_id, engine=db.get_bind())
    db.expire_all()
    linked = db.get(AIProviderCall, orphan)
    assert linked.analysis_id == analysis_id
    assert db.get(AIAnalysis, linked.analysis_id) is not None


def test_cached_part_is_reused_after_independent_commit(db, monkeypatch) -> None:
    from govcon.ai.structured import run_structured_prompt
    from govcon.prompting.registry import sync_prompts
    from govcon.security.classification import DataClassification

    settings = Settings(_env_file=None, ai_proposal_budget_share=0)
    sync_prompts(db, Path(__file__).parent.parent / "src" / "govcon" / "prompts", settings=settings)
    opp = _opp(db)
    db.commit()
    calls: list[dict] = []

    class Fake:
        name = "deepseek"

        def complete(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                content=(
                    '{"summary":"Reusable committed part used on the next run.",'
                    '"source_refs":[{"quote":"See page 2 of the RFQ document."}]}'
                ),
                usage={"prompt_tokens": 30, "completion_tokens": 8},
                model="synthetic-model", provider="deepseek", latency_ms=1, finish_reason="stop",
            )

    monkeypatch.setattr("govcon.ai.structured.get_provider", lambda *a, **k: Fake())
    first = run_structured_prompt(
        db, opportunity_id=opp.id, prompt_name="solicitation_analysis",
        analysis_type="solicitation_summary",
        variables={"OPPORTUNITY_JSON": {}, "SOURCE_PACKAGE_JSON": "same pages"},
        context_manifest={"sha": "resume", "part": 1}, settings=settings,
        classification=DataClassification.PUBLIC,
    )
    second = run_structured_prompt(
        db, opportunity_id=opp.id, prompt_name="solicitation_analysis",
        analysis_type="solicitation_summary",
        variables={"OPPORTUNITY_JSON": {}, "SOURCE_PACKAGE_JSON": "same pages"},
        context_manifest={"sha": "resume", "part": 4}, settings=settings,
        classification=DataClassification.PUBLIC,
    )
    assert len(calls) == 1
    assert second.analysis.id == first.analysis.id


def test_adaptive_planner_sizes_by_output_ratio() -> None:
    from govcon.documents.chunking import AdaptivePlanner

    planner = AdaptivePlanner(list(range(8)), units=1, max_units=3, output_cap=8192, byte_budget=None)
    assert planner.next_batch() == [0]
    planner.consume(1, output_tokens=400, input_tokens=4000)
    assert planner.units == 2
    planner.consume(2, output_tokens=5000, input_tokens=2000)
    assert planner.units == 1
    planner.consume(1, output_tokens=100, input_tokens=2000, truncated=True)
    assert planner.units == 1
    assert planner.next_batch() == [planner.pending[0]]


def test_compact_extraction_expands_short_keys_and_caps_quotes() -> None:
    from govcon.compliance.schemas import (
        RequirementExtractionV1,
        expand_compact_extraction,
    )

    long_quote = "x" * 350
    expanded = expand_compact_extraction({
        "r": [{"t": "Offerors shall submit on DIBBS.", "ty": "submission", "q": long_quote, "p": 1}],
        "n": ["dense rfq"],
    })
    assert expanded["requirements"][0]["requirement_text"] == "Offerors shall submit on DIBBS."
    assert expanded["requirements"][0]["supporting_quote"] == "x" * 200
    parsed = RequirementExtractionV1.model_validate(expanded)
    assert parsed.requirements[0].supporting_quote == "x" * 200
    assert parsed.extraction_notes == ["dense rfq"]


def test_merged_summary_has_one_coverage_note() -> None:
    from govcon.enrich.summarize import (
        coverage_note,
        merge_summaries,
        strip_part_disclaimers,
    )

    assert "only part 3 of 13" not in strip_part_disclaimers(
        "Delivery is 30 days (only part 3 of 13; later pages not read)."
    )
    merged = merge_summaries([
        {"summary": "NSN 1234-00-111-2222. Only part 1 of 13.", "items": [{"description": "glove"}]},
        {"summary": "FOB origin. (only part 2 of 13)", "items": [{"description": "glove"}]},
    ])
    assert merged["summary"].count("part") == 0
    note = coverage_note({"parts": 13, "parts_sent": 2, "gaps": [{"file_id": 1}]})
    assert note == "Coverage: analysed 2 of 13 parts; some pages remain unreviewed."
    assert "only part" not in note.lower()


def test_trace_uses_stored_matrix_and_decision(db) -> None:
    from govcon.enrich.summarize import latest_solicitation_summary
    from govcon.operating.trace import opportunity_trace

    opp = _opp(db)
    db.add(ComplianceRun(
        opportunity_id=opp.id, run_type="compliance_matrix", run_version="v1",
        output_json={}, status="complete",
    ))
    db.add(BidDecision(opportunity_id=opp.id, recommendation="no_bid"))
    part = AIAnalysis(
        opportunity_id=opp.id, analysis_type="solicitation_summary",
        output_json={"summary": "only part 4 of 13"},
        generation_settings={"role": "part", "quality": "accepted"},
    )
    merged = AIAnalysis(
        opportunity_id=opp.id, analysis_type="solicitation_summary",
        output_json={"summary": "Merged RFQ summary.\n\nCoverage: analysed 13 of 13 parts."},
        generation_settings={"role": "merged", "quality": "accepted"},
    )
    db.add_all([part, merged])
    db.flush()
    stages = {stage["id"]: stage for stage in opportunity_trace(db, opp)}
    assert stages["compliance"]["status"] == "complete"
    assert "Matrix run" in stages["compliance"]["detail"]
    assert stages["bid_decision"]["status"] == "no bid"
    assert "Decision" in stages["bid_decision"]["detail"]
    assert latest_solicitation_summary(db, opp.id).id == merged.id


def test_analyze_without_pursuit_is_audited(db, client) -> None:
    user, token = _make_user(db, f"analyze-{uuid4().hex[:8]}@example.test", "owner")
    client.cookies.set("govcon_session", token)
    opp = _opp(db)
    db.commit()
    page = client.get(f"/workspace/{opp.id}?tab=overview").text
    assert "Analyze this opportunity" in page
    assert "Estimated spend for this run" in page
    assert "Start a pursuit first" not in page
    response = client.post(f"/workspace/{opp.id}/analyze")
    assert response.status_code == 303
    db.expire_all()
    assert db.scalar(select(Pursuit).where(Pursuit.opportunity_id == opp.id)) is None
    task = db.scalar(select(Task).where(Task.opportunity_id == opp.id, Task.task_type == "opportunity_review"))
    assert task is not None
    assert task.payload.get("spend_estimate_tokens", 0) >= 0
    audit = db.scalar(select(AuditEvent).where(
        AuditEvent.opportunity_id == opp.id, AuditEvent.action_type == "analysis_requested"))
    assert audit is not None
    assert audit.new_value["scope"] == "analysis_compliance"


def test_analyze_refuses_non_public_content(db, client) -> None:
    user, token = _make_user(db, f"class-{uuid4().hex[:8]}@example.test", "owner")
    client.cookies.set("govcon_session", token)
    opp = _opp(db)
    db.add(StoredFile(
        opportunity_id=opp.id, filename="internal.pdf", classification="PROPRIETARY",
        source_origin="internal_upload", extracted_text="internal pricing",
        extraction_status="success",
    ))
    db.commit()
    response = client.post(f"/workspace/{opp.id}/analyze")
    assert response.status_code == 303
    db.expire_all()
    assert db.scalar(select(Task).where(Task.opportunity_id == opp.id, Task.task_type == "opportunity_review")) is None


def test_raise_budget_requires_reason_and_shows_above_80(db, client) -> None:
    from govcon.models import AICallUsage

    user, token = _make_user(db, f"raise-{uuid4().hex[:8]}@example.test", "owner")
    client.cookies.set("govcon_session", token)
    opp = _opp(db)
    opp.ai_max_input_tokens = 100
    db.add(AICallUsage(
        opportunity_id=opp.id, purpose="solicitation_analysis", provider="deepseek",
        status="succeeded", input_tokens=85, output_tokens=1,
    ))
    db.commit()
    denied = client.post(f"/workspace/{opp.id}/raise-budget", data={"new_limit": "240000"})
    assert denied.status_code == 303
    db.expire_all()
    assert db.get(Opportunity, opp.id).ai_max_input_tokens == 100

    page = client.get(f"/workspace/{opp.id}?tab=decision").text
    assert "Raise budget for this opportunity" in page
    assert 'name="reason"' in page
    compliance = client.get(f"/workspace/{opp.id}?tab=compliance").text
    assert "Raise budget for this opportunity" in compliance


def test_budget_panel_show_raise_at_80_percent() -> None:
    from govcon.web.routes.workspace import _budget_panel

    assert _budget_panel({"limit": 100, "used": 80, "spendable": 20})["show_raise"] is True
    assert _budget_panel({"limit": 100, "used": 79, "spendable": 21})["show_raise"] is False
    assert _budget_panel({"limit": 0, "used": 0, "spendable": 0})["show_raise"] is False


def test_compliance_requirements_paginate_and_collapse(db, client) -> None:
    user, token = _make_user(db, f"reqs-{uuid4().hex[:8]}@example.test", "owner")
    client.cookies.set("govcon_session", token)
    opp = _opp(db)
    run = ComplianceRun(
        opportunity_id=opp.id, run_type="compliance_matrix", run_version="v1",
        output_json={}, status="complete",
    )
    db.add(run)
    db.flush()
    for index in range(30):
        db.add(Requirement(
            opportunity_id=opp.id, requirement_text=f"Requirement {index + 1} shall be acknowledged.",
            requirement_type="administrative", status="unreviewed", compliance_run_id=run.id,
            source_section="Section L" if index < 15 else "Section M",
        ))
    db.commit()
    page = client.get(f"/workspace/{opp.id}?tab=compliance").text
    assert "Showing 1–25 of 30" in page
    assert 'class="req-section' in page
    assert page.count('id="req-') == 25
    page2 = client.get(f"/workspace/{opp.id}?tab=compliance&requirements_page=2").text
    assert "Showing 26–30 of 30" in page2
    assert page2.count('id="req-') == 5


def test_spend_guard_stops_at_twice_estimate() -> None:
    from govcon.ai.spend_guard import (
        SpendGuard,
        SpendGuardExceeded,
        hold_spend_guard,
        note_spend,
    )

    guard = SpendGuard(estimate_usd=Decimal("1.00"), estimate_tokens=1000)
    guard.charge(cost_usd=Decimal("1.50"), tokens=500)
    with pytest.raises(SpendGuardExceeded, match="2×"):
        guard.charge(cost_usd=Decimal("0.60"), tokens=100)
    with hold_spend_guard(0, 100):
        note_spend(tokens=100)
        with pytest.raises(SpendGuardExceeded):
            note_spend(tokens=101)


def test_estimate_review_skips_cached_parts(db) -> None:
    from govcon.ai.spend_guard import estimate_review

    opp = _opp(db)
    stored = StoredFile(
        opportunity_id=opp.id, filename="rfq.pdf", classification="PUBLIC",
        source_origin="dibbs", extracted_text="page", extraction_status="success",
        mime_type="application/pdf",
    )
    db.add(stored)
    db.flush()
    for page_no in range(1, 4):
        db.add(FilePage(file_id=stored.id, page_no=page_no, text=f"page {page_no}", text_source="native", char_count=6))
    db.add(AIAnalysis(
        opportunity_id=opp.id, analysis_type="solicitation_summary",
        output_json={"summary": "cached"}, generation_settings={"role": "part"},
    ))
    db.flush()
    estimate = estimate_review(db, opp.id, Settings(_env_file=None))
    assert estimate["pages"] == 3
    assert estimate["cached_parts"] == 1
    assert estimate["calls"] == 2
    assert estimate["cap_tokens"] == estimate["tokens"] * 2
