"""Roadmap gap 3 (stage 2C): extraction passes cover the whole document set (ADR-066)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.compliance.extractor import run_ai_pass
from govcon.compliance.records import Inventory, SourceDocument
from govcon.config import Settings
from govcon.models import Opportunity


@pytest.fixture(autouse=True)
def synced_prompts(upgraded_engine):
    """Per test: conftest relaxes the behavioral-evaluation gate per test, before this runs."""
    from govcon.prompting.registry import sync_prompts
    with Session(upgraded_engine) as session:
        sync_prompts(session, Path(__file__).parent.parent / "src" / "govcon" / "prompts")
        session.commit()


@pytest.fixture()
def db(upgraded_engine):
    with Session(upgraded_engine) as session:
        yield session
        session.rollback()


def provider(monkeypatch, *, usage):
    calls = []

    class Fake:
        name = "deepseek"

        def complete(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(content=json.dumps({"requirements": []}), usage=usage,
                                   model="synthetic", provider="deepseek", latency_ms=1)

    monkeypatch.setattr("govcon.ai.structured.get_provider", lambda *a, **k: Fake())
    return calls


def long_solicitation(db):
    opp = Opportunity(source="sam", source_id=uuid4().hex, title="Long solicitation", status="open", raw={}, links={})
    db.add(opp)
    db.flush()
    pages = [f"PAGE-MARK-{n:03d} " + f"The contractor shall meet clause {n}. " * 60 for n in range(1, 41)]
    document = SourceDocument(
        file_id=900_000 + opp.id, filename="rfq.pdf", url=None, sha256="x" * 64, downloaded_at=None,
        snapshot_id=None, document_type="solicitation", mime_type="application/pdf", text="\n\n".join(pages),
        page_texts=pages, page_count=len(pages), text_extraction_status="success", classification="PUBLIC",
    )
    return opp, Inventory(documents=[document], warnings=[])


def test_every_page_is_sent_across_as_many_calls_as_needed(db, monkeypatch):
    from govcon.ai.budget import input_bound
    opp, inventory = long_solicitation(db)
    calls = provider(monkeypatch, usage={"prompt_tokens": 100, "completion_tokens": 10})
    configured = Settings(_env_file=None)
    outcome = run_ai_pass(db, opp, inventory, "A", settings=configured)
    assert outcome.status == "complete" and len(calls) > 1
    sent = "".join(c["user_prompt"] for c in calls)
    assert all(f"PAGE-MARK-{n:03d}" in sent for n in range(1, 41)), "no page is dropped"
    assert all(input_bound(c["system_prompt"], c["user_prompt"]) <= configured.ai_max_input_tokens_per_call for c in calls)
    from govcon.models import ComplianceRun
    run = db.get(ComplianceRun, outcome.run_id)
    coverage = run.output_json["coverage"]
    assert coverage["chunks_sent"] == coverage["chunks_total"] and coverage["gaps"] == []
    assert len(run.output_json["ai_analysis_ids"]) == len(calls)


def test_exhausted_budget_makes_the_pass_incomplete_with_named_pages(db, monkeypatch):
    opp, inventory = long_solicitation(db)
    calls = provider(monkeypatch, usage={})  # unreported usage keeps the full reservation
    configured = Settings(_env_file=None, ai_max_input_tokens_per_opportunity=60_000)
    outcome = run_ai_pass(db, opp, inventory, "A", settings=configured)
    assert outcome.status == "incomplete" and calls
    from govcon.models import ComplianceRun
    run = db.get(ComplianceRun, outcome.run_id)
    assert run.status == "incomplete"
    [gap] = run.output_json["coverage"]["gaps"]
    assert gap["reason"] == "budget_exhausted" and gap["pages"] and gap["pages"][-1] == 40
    warning = next(w for w in outcome.warnings if w["code"] == "context_truncated")
    assert "rfq.pdf pages" in warning["message"] and "not extracted" in warning["message"]


def test_a_pass_with_no_document_text_fails_cleanly_without_a_call(db, monkeypatch):
    opp = Opportunity(source="dibbs", source_id=uuid4().hex, title="No documents", status="open", raw={}, links={})
    db.add(opp)
    db.flush()
    calls = provider(monkeypatch, usage={})
    for label in ("A", "B"):
        outcome = run_ai_pass(db, opp, Inventory(documents=[], warnings=[]), label, settings=Settings(_env_file=None))
        assert outcome.status == "failed" and outcome.candidates == []
        assert any(w["code"] == f"pass_{label.lower()}_no_source_text" for w in outcome.warnings)
        from govcon.models import ComplianceRun
        run = db.get(ComplianceRun, outcome.run_id)
        assert run.status == "failed" and run.output_json["coverage"]["chunks_total"] == 0
    assert calls == []


def test_compliance_pipeline_on_an_opportunity_without_documents_is_incomplete_not_an_error(db, monkeypatch):
    from govcon.compliance.pipeline import run_compliance_pipeline

    opp = Opportunity(source="dibbs", source_id=uuid4().hex, title="No documents", status="open", raw={}, links={})
    db.add(opp)
    db.flush()
    provider(monkeypatch, usage={})
    result = run_compliance_pipeline(db, opp.id, use_ai=True, settings=Settings(_env_file=None))
    assert result["status"] == "incomplete"
    passes = result["extraction"]["passes"]
    assert passes["A"]["status"] == passes["B"]["status"] == "failed"


def _stopping_provider(monkeypatch, content: str):
    calls = []

    class AtCap:
        name = "deepseek"

        def complete(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(content=content, usage={"prompt_tokens": 100, "completion_tokens": 10},
                                   model="synthetic", provider="deepseek",
                                   latency_ms=1, finish_reason="length")

    monkeypatch.setattr("govcon.ai.structured.get_provider", lambda *a, **k: AtCap())
    return calls


def test_output_cut_off_at_the_token_cap_is_split_and_retried(db, monkeypatch):
    """A part that hits the output cap is split into smaller calls instead of being discarded."""
    calls = _stopping_provider(monkeypatch, '{"requirements": [{"requirement_text": "The contractor sh')
    opp, inventory = long_solicitation(db)
    outcome = run_ai_pass(db, opp, inventory, "A", settings=Settings(_env_file=None))
    assert outcome.status == "failed"
    warning = next(w for w in outcome.warnings if w["code"] == "pass_a_output_truncated")
    assert "AI_MAX_OUTPUT_TOKENS_PER_CALL" in warning["message"]
    from govcon.models import AIProviderCall, ComplianceRun
    parts = db.get(ComplianceRun, outcome.run_id).output_json["manifest"]["parts"]
    assert parts > 1 and len(calls) > parts  # truncated parts were split and retried
    rows = db.scalars(select(AIProviderCall).where(AIProviderCall.opportunity_id == opp.id)).all()
    assert any(row.status == "truncated" and row.finish_reason == "length" for row in rows)
    assert all(row.status != "succeeded" for row in rows)


def test_a_complete_answer_that_reports_the_cap_is_still_accepted(db, monkeypatch):
    _stopping_provider(monkeypatch, json.dumps({"requirements": []}))
    opp, inventory = long_solicitation(db)
    assert run_ai_pass(db, opp, inventory, "A", settings=Settings(_env_file=None)).status == "complete"


def test_a_truncated_output_fails_the_task_with_a_next_action():
    from govcon.ai.structured import StructuredCallError
    from govcon.tasks.errors import classify

    outcome = classify(StructuredCallError("output_truncated", "stopped at the cap"))
    assert outcome.kind == "fail" and "AI_MAX_OUTPUT_TOKENS_PER_CALL" in outcome.next_action


def test_summary_reports_why_it_did_not_run(db):
    from govcon.enrich.summarize import run_solicitation_analysis

    opp = Opportunity(source="sam", source_id=uuid4().hex, title="No text", status="open", raw={}, links={})
    db.add(opp)
    db.flush()
    refusals: list[str] = []
    assert run_solicitation_analysis(db, opp, settings=Settings(_env_file=None), refusals=refusals) is None
    assert refusals == ["no document text is available"]


def test_source_batch_size_is_set_apart_from_the_per_call_input_limit(db, monkeypatch):
    """Reconciliation needs a high per-call limit; that must not make extraction parts (and answers) bigger."""
    def parts(**overrides):
        calls = provider(monkeypatch, usage={"prompt_tokens": 100, "completion_tokens": 10})
        opp, inventory = long_solicitation(db)
        run_ai_pass(db, opp, inventory, "A", settings=Settings(_env_file=None, **overrides))
        return len(calls)

    default = parts()
    assert parts(ai_max_input_tokens_per_call=400_000, ai_max_input_tokens_per_opportunity=2_000_000) == default
    assert parts(ai_source_batch_bytes=8_000, ai_max_input_tokens_per_opportunity=2_000_000) > default


def test_one_unusable_answer_leaves_a_gap_for_that_part_only(db, monkeypatch):
    """Pass B lost pages 11-19 of a real RFQ because one bad reply ended the whole pass."""
    calls = []

    class FlakySecondPart:
        name = "deepseek"

        def complete(self, **kwargs):
            calls.append(kwargs)
            bad = len(calls) in (2, 3)  # both attempts at part 2
            return SimpleNamespace(content="not json" if bad else json.dumps({"requirements": []}),
                                   usage={"prompt_tokens": 100, "completion_tokens": 10}, model="synthetic",
                                   provider="deepseek", latency_ms=1, finish_reason="stop")

    monkeypatch.setattr("govcon.ai.structured.get_provider", lambda *a, **k: FlakySecondPart())
    opp, inventory = long_solicitation(db)
    outcome = run_ai_pass(db, opp, inventory, "A", settings=Settings(_env_file=None))
    from govcon.models import ComplianceRun
    run = db.get(ComplianceRun, outcome.run_id)
    coverage = run.output_json["coverage"]
    assert outcome.status == "incomplete"
    assert coverage["chunks_sent"] < coverage["chunks_total"] and coverage["chunks_sent"] > 0
    [gap] = coverage["gaps"]
    assert gap["reason"] == "invalid_output" and 1 not in gap["pages"] and 40 not in gap["pages"]
    sent = "".join(c["user_prompt"] for c in calls[3:])
    assert "PAGE-MARK-040" in sent  # the parts after the bad one were still read


# ── contradiction scan in parts (a large RFQ's requirements exceeded one call) ──

def _requirements(db, opp, counts: dict[str, int], text_len: int = 400):
    from govcon.models import Requirement

    rows = []
    for kind, n in counts.items():
        for i in range(n):
            rows.append(Requirement(opportunity_id=opp.id, requirement_type=kind, status="unknown",
                                    requirement_text=f"{kind} {i} " + "x" * text_len))
    db.add_all(rows)
    db.flush()
    return rows


def test_contradiction_parts_keep_each_type_together_and_cover_everything(db):
    from govcon.compliance.conflicts import contradiction_parts

    opp, _ = long_solicitation(db)
    rows = _requirements(db, opp, {"delivery": 6, "packaging": 3, "quality": 2, "technical": 30})
    parts = contradiction_parts(rows, max_bytes=6_000)
    assert sorted(r.id for part in parts for r in part) == sorted(r.id for r in rows)  # each exactly once
    for kind in ("delivery", "packaging", "quality"):  # small types never straddle parts
        assert sum(any(r.requirement_type == kind for r in part) for part in parts) == 1
    assert sum(any(r.requirement_type == "technical" for r in part) for part in parts) > 1  # only the oversized type splits


def test_contradiction_scan_of_a_large_requirement_set_runs_in_parts(db, monkeypatch):
    from govcon.compliance.conflicts import run_conflict_scan
    from govcon.compliance.records import Inventory

    calls = []

    class Fake:
        name = "deepseek"

        def complete(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(content=json.dumps({"conflicts": []}), usage={"prompt_tokens": 100, "completion_tokens": 10},
                                   model="synthetic", provider="deepseek", latency_ms=1, finish_reason="stop")

    monkeypatch.setattr("govcon.ai.structured.get_provider", lambda *a, **k: Fake())
    opp, _ = long_solicitation(db)
    _requirements(db, opp, {"delivery": 40, "technical": 60, "certification": 30})
    settings = Settings(_env_file=None, ai_max_input_tokens_per_call=30_000, ai_max_input_tokens_per_opportunity=2_000_000)
    result = run_conflict_scan(db, opp.id, Inventory(documents=[], warnings=[]), use_ai=True, settings=settings)
    assert result["ai"]["status"] == "complete" and result["ai"]["parts"] == len(calls) > 1
    from govcon.models import ComplianceRun
    assert not (db.get(ComplianceRun, result["run_id"]).warnings or [])  # nothing over the per-call limit
