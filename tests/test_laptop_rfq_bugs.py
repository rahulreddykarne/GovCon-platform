"""Laptop E2E bugs from opp 10497 (DLA SPE7L127U0016): truncation, linking, empty UNKNOWN files."""

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
from govcon.models import AIProviderCall, Opportunity, StoredFile


@pytest.fixture(autouse=True)
def synced_prompts(upgraded_engine):
    from govcon.prompting.registry import sync_prompts
    with Session(upgraded_engine) as session:
        sync_prompts(session, Path(__file__).parent.parent / "src" / "govcon" / "prompts")
        session.commit()


@pytest.fixture()
def db(upgraded_engine):
    with Session(upgraded_engine) as session:
        yield session
        session.rollback()


def _settings(**values) -> Settings:
    return Settings(_env_file=None, **values)


def _opportunity(db) -> Opportunity:
    opp = Opportunity(source="dibbs", source_id=uuid4().hex, title="DLA RFQ", status="open", raw={}, links={})
    db.add(opp)
    db.flush()
    return opp


def _provider(monkeypatch, *, content, finish_reason="stop", name="deepseek"):
    calls = []

    class Fake:
        def complete(self, **kwargs):
            calls.append(kwargs)
            body = content(len(calls)) if callable(content) else content
            return SimpleNamespace(
                content=body,
                usage={"prompt_tokens": 80, "completion_tokens": 12},
                model="synthetic",
                provider=name,
                latency_ms=1,
                finish_reason=finish_reason(len(calls)) if callable(finish_reason) else finish_reason,
            )

    Fake.name = name
    monkeypatch.setattr("govcon.ai.structured.get_provider", lambda *a, **k: Fake())
    return calls


def _document(*, file_id, filename, text, classification, status, error=None):
    return SourceDocument(
        file_id=file_id, filename=filename, url=None, sha256="a" * 64 if text else None,
        downloaded_at=None, snapshot_id=None, document_type="solicitation", mime_type="application/pdf",
        text=text, page_texts=[text] if text else None, page_count=1 if text else 0,
        text_extraction_status=status, classification=classification, extraction_error=error,
    )


def test_failed_download_is_a_gap_not_an_unknown_classification_block(db, monkeypatch):
    """A 0-byte UNKNOWN SAM/DIBBS failure must not refuse PUBLIC extraction as UNKNOWN."""
    _provider(monkeypatch, content=json.dumps({"requirements": []}))
    opp = _opportunity(db)
    public = StoredFile(
        opportunity_id=opp.id, filename="rfq.pdf", classification="PUBLIC",
        source_origin="government_feed", extracted_text="The contractor shall deliver NSN 123.",
        extraction_status="success", active=True, sha256="b" * 64,
    )
    failed = StoredFile(
        opportunity_id=opp.id, filename="sam-description.json", classification="UNKNOWN",
        source_origin="government_feed", extraction_status="download_failed",
        extraction_error="download failed: HTTP 429", active=True,
    )
    db.add_all([public, failed])
    db.flush()
    inventory = Inventory(documents=[
        _document(file_id=public.id, filename=public.filename, text=public.extracted_text,
                  classification="PUBLIC", status="success"),
        _document(file_id=failed.id, filename=failed.filename, text=None,
                  classification="UNKNOWN", status="download_failed", error=failed.extraction_error),
    ], warnings=[])
    outcome = run_ai_pass(db, opp, inventory, "A", settings=_settings())
    assert outcome.status == "incomplete"
    assert any(w["code"] == "source_ingestion_incomplete" for w in outcome.warnings)
    from govcon.models import ComplianceRun
    gaps = db.get(ComplianceRun, outcome.run_id).output_json["coverage"]["gaps"]
    assert any(gap["file_id"] == failed.id and gap["reason"] == "download_failed" for gap in gaps)
    rows = db.scalars(select(AIProviderCall).where(AIProviderCall.opportunity_id == opp.id)).all()
    assert rows and all(row.status != "blocked" for row in rows)
    assert all("UNKNOWN" not in (row.finish_reason or "") for row in rows)


def test_sendable_unknown_content_still_blocks_and_writes_a_ledger_row(db, monkeypatch):
    calls = _provider(monkeypatch, content=json.dumps({"requirements": []}))
    opp = _opportunity(db)
    unknown = StoredFile(
        opportunity_id=opp.id, filename="unlabeled.pdf", classification="UNKNOWN",
        source_origin="government_feed", extracted_text="The contractor shall mark this package.",
        extraction_status="success", active=True, sha256="c" * 64,
    )
    db.add(unknown)
    db.flush()
    inventory = Inventory(documents=[
        _document(file_id=unknown.id, filename=unknown.filename, text=unknown.extracted_text,
                  classification="UNKNOWN", status="success"),
    ], warnings=[])
    outcome = run_ai_pass(db, opp, inventory, "A", settings=_settings())
    assert outcome.status == "failed"
    assert any("does not allow UNKNOWN" in w["message"] for w in outcome.warnings)
    assert calls == []
    rows = db.scalars(select(AIProviderCall).where(AIProviderCall.opportunity_id == opp.id)).all()
    assert len(rows) == 1
    assert rows[0].status == "blocked"
    assert "UNKNOWN" in (rows[0].finish_reason or "")


def test_merged_summary_links_every_part_to_the_analysis(db, monkeypatch):
    from govcon.enrich.summarize import run_solicitation_analysis

    _provider(monkeypatch, content=json.dumps({"summary": "DLA RFQ for a spare part."}))
    opp = _opportunity(db)
    for index in range(2):
        db.add(StoredFile(
            opportunity_id=opp.id, filename=f"part-{index}.txt", classification="PUBLIC",
            source_origin="government_feed", extracted_text=f"source{index} " * 4000,
            extraction_status="success", active=True, sha256=f"{index}" * 64,
        ))
    db.flush()
    analysis = run_solicitation_analysis(db, opp, settings=_settings(), force=True)
    assert analysis is not None
    rows = db.scalars(select(AIProviderCall).where(AIProviderCall.opportunity_id == opp.id)).all()
    assert len(rows) > 1
    assert all(row.analysis_id == analysis.id for row in rows)
    assert all(row.status == "succeeded" for row in rows)


def test_contradiction_truncated_part_is_split_and_retried(db, monkeypatch):
    from govcon.compliance.conflicts import run_conflict_scan
    from govcon.models import Requirement

    def content(n):
        if n == 1:
            return '{"conflicts": [{"topic": "delivery'
        return json.dumps({"conflicts": []})

    def reason(n):
        return "length" if n == 1 else "stop"

    calls = _provider(monkeypatch, content=content, finish_reason=reason)
    opp = _opportunity(db)
    db.add_all([
        Requirement(
            opportunity_id=opp.id, requirement_type="delivery", status="unknown",
            requirement_text=f"Deliver lot {index} " + "x" * 200,
        )
        for index in range(8)
    ])
    db.flush()
    result = run_conflict_scan(
        db, opp.id, Inventory(documents=[], warnings=[]), use_ai=True,
        settings=_settings(ai_source_batch_bytes=8_000),
    )
    assert result["ai"]["status"] in {"complete", "partial"}
    assert len(calls) > result["ai"]["parts"] or result["ai"]["parts"] > 1
    rows = db.scalars(select(AIProviderCall).where(AIProviderCall.purpose == "contradiction_detection")).all()
    assert any(row.status == "truncated" and row.finish_reason == "length" for row in rows)
    assert any(row.status == "succeeded" for row in rows)
