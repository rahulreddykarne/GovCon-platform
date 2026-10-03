"""Roadmap gap 3 (stage 2C): extraction passes cover the whole document set (ADR-066)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
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
