"""Regression checks for the production-readiness audit findings."""

from __future__ import annotations

import pytest
from test_preparation import client as client
from test_preparation import db as db
from test_preparation import default_settings as default_settings
from test_preparation import new_opportunity

from govcon.config import get_settings
from govcon.models import Task


@pytest.mark.parametrize("kind", ["pdf", "txt"])
def test_unreadable_sources_skip_summary_and_advance_preparation(db, tmp_path, monkeypatch, kind):
    from govcon.enrich.attachments import process_local_file
    from govcon.enrich.summarize import run_solicitation_analysis
    from govcon.security.classification import DataClassification
    from govcon.tasks.worker import run_once
    from govcon.workflow.preparation import queue_preparation

    monkeypatch.setenv("OCR_ENABLED", "false")
    get_settings.cache_clear()
    opportunity = new_opportunity(db)
    path = tmp_path / f"unreadable.{kind}"
    if kind == "pdf":
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        writer.write(path)
    else:
        path.write_text(" \n\t", encoding="utf-8")
    process_local_file(
        db, opportunity, path,
        classification=DataClassification.PUBLIC, source_origin="synthetic regression fixture",
    )
    db.commit()
    refusals = []
    assert run_solicitation_analysis(db, opportunity, refusals=refusals) is None
    assert len(refusals) == 1 and "readable text" in refusals[0] and "OCR" in refusals[0]
    task, _ = queue_preparation(db, opportunity_id=opportunity.id, actor_user_id=None)
    task.checkpoint = {"completed_steps": ["documents"]}
    db.commit()
    run_once(task_id=task.id)
    db.expire_all()
    task = db.get(Task, task.id)
    assert "summary" in task.checkpoint["completed_steps"]
    assert "list index out of range" not in (task.last_error or "")


@pytest.mark.parametrize("sam_cached", [False, True])
def test_ocr_retry_reuses_file_and_repairs_pages_without_refetching_sam(db, tmp_path, monkeypatch, sam_cached):
    import io
    from types import SimpleNamespace

    import httpx
    from pypdf import PdfWriter
    from sqlalchemy import select

    from govcon.config import Settings
    from govcon.enrich.attachments import download_attachments
    from govcon.enrich.extract import ExtractionResult, PageText
    from govcon.models import FilePage, OpportunitySnapshot
    from govcon.workflow.preparation import _documents_prepare
    from govcon.workflow.source_revision import current_source_revision

    opportunity = new_opportunity(db)
    url = "https://api.sam.gov/resources/rfq.pdf" if sam_cached else "https://files.example.test/rfq.pdf"
    opportunity.links = {"attachments": [url]}
    opportunity.raw_hash = "synthetic-current-notice"
    db.add(OpportunitySnapshot(opportunity_id=opportunity.id, content_hash=opportunity.raw_hash, raw={}))
    db.commit()
    stream = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(stream)
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, content=stream.getvalue(), headers={"content-type": "application/pdf"})

    disabled = Settings(_env_file=None, ocr_enabled=False, data_dir=tmp_path)
    enabled = Settings(_env_file=None, ocr_enabled=True, data_dir=tmp_path)
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        first = download_attachments(db, opportunity, settings=disabled, client=http, resolver=lambda _: ["93.184.216.34"])[0]
        db.commit()
        assert first.extracted_text is None
        previous_revision = current_source_revision(db, opportunity.id)
        docs = _documents_prepare(db, SimpleNamespace(opportunity_id=opportunity.id), SimpleNamespace(settings=enabled))
        assert first.id in [item["id"] for item in docs.backfill]
        calls = []

        def extract(*args, **kwargs):
            calls.append(True)
            return ExtractionResult("Contractor shall deliver 100 EA.", "success",
                pages=[PageText(1, "Contractor shall deliver 100 EA.", "ocr")], ocr_pages=[1], page_count=1)

        monkeypatch.setattr("govcon.enrich.attachments.extract_text", extract)
        repaired = download_attachments(db, opportunity, settings=enabled, client=http, resolver=lambda _: ["93.184.216.34"])[0]
        db.commit()
        assert repaired.id == first.id and repaired.extraction_status == "success"
        assert repaired.extracted_text == "Contractor shall deliver 100 EA."
        assert len(calls) == 1 and len(seen) == (1 if sam_cached else 2)
        pages = db.scalars(select(FilePage).where(FilePage.file_id == repaired.id)).all()
        assert len(pages) == 1 and pages[0].text_source == "ocr"
        repaired_revision = current_source_revision(db, opportunity.id)
        assert repaired_revision != previous_revision
        from govcon.workflow.source_revision import is_stale

        assert is_stale(previous_revision.legacy, repaired_revision)
        assert _documents_prepare(db, SimpleNamespace(opportunity_id=opportunity.id), SimpleNamespace(settings=enabled)).backfill == []


def test_ocr_repair_keeps_previously_readable_pages(db, tmp_path):
    from govcon.enrich.attachments import update_extraction, write_pages
    from govcon.enrich.extract import ExtractionResult, PageText
    from govcon.models import StoredFile

    opportunity = new_opportunity(db)
    row = StoredFile(opportunity_id=opportunity.id, filename="scan.pdf", mime_type="application/pdf",
                     classification="CUI", source_origin="synthetic internal fixture",
                     extracted_text="Previously read first page.", extraction_status="partial")
    db.add(row)
    db.flush()
    write_pages(db, row, ExtractionResult(row.extracted_text, "partial", page_count=2,
        pages=[PageText(1, row.extracted_text, "ocr"), PageText(2, "", "none")],
        ocr_pages=[1], ocr_failed_pages=[{"page": 2, "reason": "unreadable"}]))
    update_extraction(db, row, ExtractionResult("Newly read second page.", "partial", page_count=2,
        pages=[PageText(1, "", "none"), PageText(2, "Newly read second page.", "ocr")],
        ocr_pages=[2], ocr_failed_pages=[{"page": 1, "reason": "unreadable"}]))
    assert "Previously read first page." in row.extracted_text
    assert "Newly read second page." in row.extracted_text
    assert row.extraction_status == "success" and not row.ocr_failed_pages
    assert row.classification == "CUI"


def test_concurrent_ocr_repairs_keep_both_pages(upgraded_engine, db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Event

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from govcon.enrich.attachments import update_extraction, write_pages
    from govcon.enrich.extract import ExtractionResult, PageText
    from govcon.models import FilePage, StoredFile

    opportunity = new_opportunity(db)
    row = StoredFile(opportunity_id=opportunity.id, filename="scan.pdf", mime_type="application/pdf",
                     classification="PUBLIC", extraction_status="partial")
    db.add(row)
    db.flush()
    write_pages(db, row, ExtractionResult(None, "partial", page_count=2,
        pages=[PageText(1, "", "none"), PageText(2, "", "none")],
        ocr_failed_pages=[{"page": 1}, {"page": 2}]))
    db.commit()
    file_id = row.id
    loaded = Barrier(2)
    first_deleted = Event()
    second_delete_attempted = Event()

    class CoordinatedSession(Session):
        def execute(self, statement, *args, **kwargs):
            deleting_pages = getattr(statement, "is_delete", False) and statement.table.name == "file_pages"
            if deleting_pages and self.info["repair_page"] == 2:
                second_delete_attempted.set()
            result = super().execute(statement, *args, **kwargs)
            if deleting_pages and self.info["repair_page"] == 1:
                first_deleted.set()
                # An unlocked repair reaches DELETE here; a locked one waits
                # for the first transaction and must let it finish instead.
                second_delete_attempted.wait(timeout=1)
            return result

    def repair(page_no):
        with CoordinatedSession(upgraded_engine, autoflush=False, info={"repair_page": page_no}) as session:
            stored = session.get(StoredFile, file_id)
            loaded.wait(timeout=5)
            if page_no == 2:
                assert first_deleted.wait(timeout=5)
            update_extraction(session, stored, ExtractionResult(f"Read page {page_no}.", "partial", page_count=2,
                pages=[PageText(number, f"Read page {number}." if number == page_no else "",
                                "ocr" if number == page_no else "none") for number in (1, 2)],
                ocr_pages=[page_no], ocr_failed_pages=[{"page": 3 - page_no}]))
            session.commit()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(repair, number) for number in (1, 2)]
        for future in futures:
            future.result(timeout=10)
    db.expire_all()
    stored = db.get(StoredFile, file_id)
    assert "Read page 1." in stored.extracted_text and "Read page 2." in stored.extracted_text
    assert stored.extraction_status == "success" and not stored.ocr_failed_pages
    pages = db.scalars(select(FilePage).where(FilePage.file_id == file_id).order_by(FilePage.page_no)).all()
    assert [(page.page_no, page.text) for page in pages] == [(1, "Read page 1."), (2, "Read page 2.")]


@pytest.fixture()
def approved_prompts(upgraded_engine):
    from pathlib import Path

    from sqlalchemy.orm import Session

    from govcon.prompting.registry import sync_prompts

    with Session(upgraded_engine) as session:
        sync_prompts(session, Path(__file__).parents[1] / "src" / "govcon" / "prompts")
        session.commit()


@pytest.mark.parametrize("payload", [
    {}, {"unrecognized_provider_key": "ignored"}, {"summary": " \n"},
    {"items": [{}]}, {"delivery": {}}, {"source_refs": [{"page": 1}]},
])
def test_empty_summary_output_is_rejected(payload):
    from pydantic import ValidationError

    from govcon.ai.schemas import SolicitationAnalysisV1

    with pytest.raises(ValidationError):
        SolicitationAnalysisV1.model_validate(payload)


def test_empty_summary_is_not_saved_and_old_empty_cache_is_replaced(db, tmp_path, monkeypatch, approved_prompts):
    import json
    from types import SimpleNamespace

    from sqlalchemy import select

    from govcon.ai.analysis_types import AnalysisType
    from govcon.config import Settings
    from govcon.enrich.attachments import process_local_file
    from govcon.enrich.summarize import run_solicitation_analysis
    from govcon.models import AIAnalysis
    from govcon.security.classification import DataClassification
    from govcon.workflow.source_revision import current_source_revision

    opportunity = new_opportunity(db)
    path = tmp_path / "solicitation.txt"
    path.write_text("Contractor shall deliver 100 filters.", encoding="utf-8")
    process_local_file(db, opportunity, path, classification=DataClassification.PUBLIC,
                       source_origin="synthetic regression fixture")
    db.commit()
    payload = {}
    calls = []

    class Provider:
        name = "deepseek"

        def complete(self, **kwargs):
            calls.append(True)
            return SimpleNamespace(content=json.dumps(payload), usage={"prompt_tokens": 100, "completion_tokens": 10},
                                   provider="deepseek", model="synthetic", finish_reason="stop", latency_ms=1)

    monkeypatch.setattr("govcon.ai.structured.get_provider", lambda *args, **kwargs: Provider())
    settings = Settings(_env_file=None, prompt_max_retries_on_invalid_json=0)
    refusals = []
    assert run_solicitation_analysis(db, opportunity, settings=settings, refusals=refusals) is None
    assert refusals and "invalid_output" in refusals[0]
    assert db.scalar(select(AIAnalysis.id).where(AIAnalysis.opportunity_id == opportunity.id)) is None
    legacy = AIAnalysis(opportunity_id=opportunity.id, analysis_type=AnalysisType.SOLICITATION_SUMMARY,
                        schema_version="solicitation_analysis.v1", output_json={"summary": None, "items": []},
                        context_manifest={"source_revision": current_source_revision(db, opportunity.id)})
    db.add(legacy)
    db.commit()
    payload = {"summary": "The contractor must deliver 100 filters."}
    fresh = run_solicitation_analysis(db, opportunity, settings=settings)
    assert fresh is not None and fresh.id != legacy.id and len(calls) == 2


def test_summary_product_facts_reach_rfq_quotes_pricing_and_decision(db):
    from decimal import Decimal
    from uuid import uuid4

    from test_web_ui import _make_user

    from govcon.ai.analysis_types import AnalysisType
    from govcon.config import Settings
    from govcon.decision.engine import build_decision_state
    from govcon.intelligence.ai_analyses import _pricing_request
    from govcon.models import AIAnalysis, Pursuit, Supplier
    from govcon.sourcing.records import draft_rfq, lowest_current_total, record_quote
    from govcon.workflow.source_revision import current_source_revision

    opportunity = new_opportunity(db)
    analysis = AIAnalysis(opportunity_id=opportunity.id, analysis_type=AnalysisType.SOLICITATION_SUMMARY,
        schema_version="solicitation_analysis.v1",
        output_json={"items": [{"nsn": "2910-00-123-4567", "quantity": 100, "unit": "EA"},
                               {"nsn": "2910001234567", "quantity": 100, "unit": "EA"}]},
        context_manifest={"source_revision": current_source_revision(db, opportunity.id)})
    db.add(analysis)
    actor, _ = _make_user(db, uuid4().hex + "@regression.test", "owner")
    supplier = Supplier(name="Synthetic supplier " + uuid4().hex, provenance="regression fixture")
    db.add(supplier)
    db.flush()
    quote = record_quote(db, opportunity_id=opportunity.id, supplier_id=supplier.id, actor=actor, method="csv",
                         lines=[{"quantity": 100, "unit": "EA", "unit_price": 2, "nsn": "2910001234567"}])
    db.add(Pursuit(opportunity_id=opportunity.id, stage="evaluating", quote_price=Decimal("350"),
                   sourcing_cost=Decimal("200")))
    db.commit()
    rfq = draft_rfq(db, opportunity_id=opportunity.id, supplier_id=supplier.id, actor=actor)
    assert "2910-00-123-4567" in rfq.body and "Quantity: 100 EA" in rfq.body
    assert lowest_current_total(db, opportunity.id).id == quote.id
    request = _pricing_request(db, opportunity.id, Settings(_env_file=None))
    inputs = request["variables"]["PRICING_INPUTS_JSON"]
    assert inputs["quantity"] == 100 and inputs["proposed_unit_price"] == 3.5 and inputs["unit_cost"] == 2
    state = build_decision_state(db, opportunity.id)
    assert state["pricing"]["proposed_unit_price"] == 3.5
    assert opportunity.nsn is None and opportunity.quantity is None


@pytest.mark.parametrize("case", ["multiple_products", "conflicting_quantities", "stale", "feed_wins", "unidentified_quantity"])
def test_rfq_never_combines_ambiguous_stale_or_conflicting_product_facts(db, case):
    from decimal import Decimal
    from uuid import uuid4

    from test_web_ui import _make_user

    from govcon.ai.analysis_types import AnalysisType
    from govcon.models import AIAnalysis
    from govcon.sourcing.records import draft_rfq
    from govcon.workflow.source_revision import current_source_revision

    opportunity = new_opportunity(db)
    items = [{"nsn": "2910001234567", "quantity": 100, "unit": "EA"}]
    if case == "multiple_products":
        items.append({"nsn": "2910001234568", "quantity": 100, "unit": "EA"})
    if case == "conflicting_quantities":
        items.append({"nsn": "2910001234567", "quantity": 200, "unit": "EA"})
    if case == "unidentified_quantity":
        items[0]["quantity"] = None
        items.append({"description": "Unidentified second product", "quantity": 100, "unit": "EA"})
    if case == "feed_wins":
        opportunity.nsn, opportunity.quantity, opportunity.unit = "2910001234567", Decimal("10"), "EA"
    revision = "v2:" + "0" * 64 if case == "stale" else current_source_revision(db, opportunity.id)
    db.add(AIAnalysis(opportunity_id=opportunity.id, analysis_type=AnalysisType.SOLICITATION_SUMMARY,
                     schema_version="solicitation_analysis.v1", output_json={"items": items},
                     context_manifest={"source_revision": revision}))
    actor, _ = _make_user(db, uuid4().hex + "@regression.test", "owner")
    db.commit()
    rfq = draft_rfq(db, opportunity_id=opportunity.id, supplier_id=None, actor=actor)
    assert ("Quantity: 10 EA" if case == "feed_wins" else "Quantity: not stated") in rfq.body


@pytest.mark.parametrize("case,expected", [
    ("stale", None), ("expired", False), ("expires_before_deadline", False), ("active", True),
])
def test_vendor_registration_fallback_respects_freshness_and_expiration(db, case, expected):
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from govcon.config import Settings
    from govcon.decision.signals import Signals, eligibility_signals
    from govcon.models import Vendor

    opportunity = new_opportunity(db)
    now = datetime.now(UTC)
    uei = uuid4().hex[:12].upper()
    expiration = now + timedelta(days=90)
    if case == "expired":
        expiration = now - timedelta(days=20)
    if case == "expires_before_deadline":
        expiration = now + timedelta(days=10)
    db.add(Vendor(uei=uei, fetched_at=now - timedelta(days=30) if case == "stale" else now,
                  registration_status="Active",
                  raw={"entityRegistration": {"registrationExpirationDate": expiration.date().isoformat()}}))
    db.commit()
    signals = Signals()
    eligibility_signals(db, opportunity, {"uei": uei}, {}, has_summary=False,
                        settings=Settings(_env_file=None), signals=signals)
    assert signals.values["sam_active"] is expected


@pytest.mark.parametrize("status,expected", [(None, "unknown"), ("Inactive", "fail")])
def test_fresh_registration_unknowns_replace_old_eligibility_assertions(db, status, expected):
    from datetime import UTC, datetime
    from uuid import uuid4

    from govcon.company.registration import overlay_registration
    from govcon.compliance.deterministic import sam_registration_known
    from govcon.decision.signals import Signals, eligibility_signals
    from govcon.models import CompanyRegistration, Vendor

    opportunity = new_opportunity(db)
    uei = uuid4().hex[:12].upper()
    db.add(CompanyRegistration(uei=uei, refreshed_at=datetime.now(UTC),
                              registration_status=status, expiration_date=None))
    db.add(Vendor(uei=uei, fetched_at=datetime.now(UTC), registration_status="Active", raw={}))
    db.commit()
    facts = overlay_registration(db, {"uei": uei, "sam_registration_status": "Active",
                                      "sam_expiration_date": "2030-01-01", "naics_codes": ["339112"]})
    assert facts.get("sam_expiration_date") is None
    assert sam_registration_known(facts, opportunity.response_deadline).status == expected
    assert facts["naics_codes"] == ["339112"]
    signals = Signals()
    eligibility_signals(db, opportunity, facts, {}, has_summary=False, signals=signals)
    assert signals.values["sam_active"] is (None if status is None else False)


def test_web_return_for_fix_can_save_a_revision_and_red_team_it(upgraded_engine, db, client, tmp_path, monkeypatch, approved_prompts):
    import json
    from types import SimpleNamespace

    from test_release_lifecycle import prepared_lifecycle

    from govcon.collaboration.users import create_session
    from govcon.models import Proposal, ProposalVersion, User
    from govcon.proposals.versions import get_sections_for_version

    ids, _, _ = prepared_lifecycle(upgraded_engine, tmp_path)
    opportunity_id, actor_id, proposal_id, _ = ids
    actor = db.get(User, actor_id)
    client.cookies.set("govcon_session", create_session(db, actor))
    db.commit()
    proposal = db.get(Proposal, proposal_id)
    old_version_id = proposal.current_version_id
    old_text = db.get(ProposalVersion, old_version_id).full_text
    response = client.post(f"/workspace/{opportunity_id}/proposal/approve",
                           data={"decision": "RETURN_FOR_FIX", "expected_version": proposal.version})
    assert "error=" not in response.headers["location"]
    db.expire_all()
    proposal = db.get(Proposal, proposal_id)
    html = client.get(f"/workspace/{opportunity_id}?tab=proposal").text
    assert f"/workspace/{opportunity_id}/proposal/revise" in html and "Save revision" in html
    fields = {f"section_{section.id}": section.content + "\nReviewed and corrected."
              for section in get_sections_for_version(db, old_version_id)}
    fields.update(action="save", expected_version=str(proposal.version))
    response = client.post(f"/workspace/{opportunity_id}/proposal/revise", data=fields)
    assert response.status_code == 303 and "notice=" in response.headers["location"]
    db.expire_all()
    proposal = db.get(Proposal, proposal_id)
    assert proposal.current_version_id != old_version_id and proposal.status == "draft"
    assert db.get(ProposalVersion, old_version_id).full_text == old_text
    assert "Reviewed and corrected." in db.get(ProposalVersion, proposal.current_version_id).full_text
    count = proposal.version
    response = client.post(f"/workspace/{opportunity_id}/proposal/revise", data=fields)
    assert response.status_code == 409 and "Reviewed and corrected." in response.text
    db.expire_all()
    assert db.get(Proposal, proposal_id).version == count

    class Provider:
        name = "deepseek"

        def complete(self, **kwargs):
            return SimpleNamespace(content=json.dumps({"overall_assessment": "READY", "findings": [],
                "critical_count": 0, "major_count": 0, "minor_count": 0}),
                usage={"prompt_tokens": 100, "completion_tokens": 10}, provider="deepseek",
                model="synthetic", finish_reason="stop", latency_ms=1)

    monkeypatch.setattr(client.app.state.settings, "ai_external_allowed_for_proprietary", True)
    monkeypatch.setattr("govcon.ai.structured.get_provider", lambda *args, **kwargs: Provider())
    response = client.post(f"/workspace/{opportunity_id}/proposal/revise",
                           data={"action": "red_team", "expected_version": count})
    assert response.status_code == 303 and "notice=" in response.headers["location"], __import__("re").findall(
        r'<p class="alert alert-error">(.*?)</p>', response.text
    )
    db.expire_all()
    assert db.get(Proposal, proposal_id).status == "red_teamed"


@pytest.mark.parametrize("case", ["read_only", "closed", "replacement"])
def test_web_proposal_revision_guards_and_explicit_replacement(upgraded_engine, db, client, tmp_path, case):
    from sqlalchemy import select
    from test_release_lifecycle import prepared_lifecycle
    from test_web_ui import _make_user

    from govcon.collaboration.users import create_session
    from govcon.models import Proposal, ProposalVersion, Pursuit, User

    ids, _, _ = prepared_lifecycle(upgraded_engine, tmp_path)
    opportunity_id, actor_id, proposal_id, _ = ids
    actor = db.get(User, actor_id)
    if case == "read_only":
        actor, _ = _make_user(db, f"reader-{opportunity_id}@regression.test", "read_only")
    if case == "closed":
        db.scalar(select(Pursuit).where(Pursuit.opportunity_id == opportunity_id)).stage = "cancelled"
    client.cookies.set("govcon_session", create_session(db, actor))
    db.commit()
    proposal = db.get(Proposal, proposal_id)
    previous_id, previous_text = proposal.current_version_id, db.get(ProposalVersion, proposal.current_version_id).full_text
    response = client.post(f"/workspace/{opportunity_id}/proposal/revise",
                           data={"action": "regenerate_without_ai", "expected_version": proposal.version})
    db.expire_all()
    proposal = db.get(Proposal, proposal_id)
    if case == "replacement":
        assert response.status_code == 303 and "notice=" in response.headers["location"]
        assert proposal.current_version_id != previous_id
    else:
        assert response.status_code == 400
        assert proposal.current_version_id == previous_id
    assert db.get(ProposalVersion, previous_id).full_text == previous_text


def test_web_package_download_assembly_preflight_and_manual_confirmation(upgraded_engine, db, client, tmp_path, monkeypatch):
    import io
    import zipfile

    from docx import Document
    from test_release_lifecycle import prepared_lifecycle

    from govcon.collaboration.users import create_session
    from govcon.models import Proposal, Submission, User
    from govcon.submissions.manifest import current_package

    ids, _, _ = prepared_lifecycle(upgraded_engine, tmp_path / "source")
    opportunity_id, actor_id, proposal_id, submission_id = ids
    monkeypatch.setattr(client.app.state.settings, "data_dir", tmp_path / "uploads")
    client.cookies.set("govcon_session", create_session(db, db.get(User, actor_id)))
    db.commit()
    proposal, submission = db.get(Proposal, proposal_id), db.get(Submission, submission_id)
    assert "Assemble package" in client.get(f"/workspace/{opportunity_id}?tab=submission").text
    downloaded = {}
    for fmt in ("docx", "pdf", "xlsx"):
        response = client.post(f"/workspace/{opportunity_id}/submission/package/export",
                               data={"format": fmt, "expected_proposal_version": proposal.version})
        assert response.status_code == 200 and "attachment;" in response.headers["content-disposition"]
        downloaded[fmt] = response.content
    assert Document(io.BytesIO(downloaded["docx"])).paragraphs
    assert downloaded["pdf"].startswith(b"%PDF") and zipfile.is_zipfile(io.BytesIO(downloaded["xlsx"]))
    fields = {"expected_proposal_version": str(proposal.version), "expected_submission_version": str(submission.version),
              "proposal_filename": "proposal.docx", "pricing_filenames": "pricing.xlsx", "pricing_rows": "0001,2,100"}
    uploaded = [("files", ("proposal.docx", downloaded["docx"])),
                ("files", ("pricing.xlsx", (tmp_path / "source" / "pricing.xlsx").read_bytes()))]
    response = client.post(f"/workspace/{opportunity_id}/submission/package/assemble", data=fields, files=uploaded)
    assert response.status_code == 303 and "notice=" in response.headers["location"], response.headers.get("location")
    db.expire_all()
    submission = db.get(Submission, submission_id)
    package = current_package(db, submission)
    assert package and package.files[0].local_path.startswith(str(tmp_path / "uploads"))
    assert package.files[0].sha256
    package_hash = submission.package_manifest_hash
    # Retry with the obsolete form must not overwrite the new package.
    response = client.post(f"/workspace/{opportunity_id}/submission/package/assemble", data=fields, files=uploaded)
    assert "error=" in response.headers["location"]
    db.expire_all()
    assert db.get(Submission, submission_id).package_manifest_hash == package_hash
    fields["expected_submission_version"] = str(submission.version)
    response = client.post(f"/workspace/{opportunity_id}/submission/package/preflight", data=fields)
    assert "Preflight passed" in client.get(response.headers["location"]).text
    db.expire_all()
    proposal, submission = db.get(Proposal, proposal_id), db.get(Submission, submission_id)
    response = client.post(f"/workspace/{opportunity_id}/submission/package/export", data={**fields, "format": "zip"})
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.read("proposal.docx") == downloaded["docx"]
        assert "pricing.xlsx" in archive.namelist()
    response = client.post(f"/workspace/{opportunity_id}/proposal/approve",
                           data={"decision": "APPROVE_FOR_SUBMISSION", "expected_version": proposal.version})
    assert "error=" not in response.headers["location"], response.headers.get("location")
    db.expire_all()
    submission = db.get(Submission, submission_id)
    response = client.post(f"/workspace/{opportunity_id}/submission/approve",
                           data={"expected_version": submission.version, "confirmation_number": "SYNTHETIC-WEB-ONLY",
                                 "confirmation_notes": "Synthetic local test; nothing was submitted externally."})
    assert "error=" not in response.headers["location"], response.headers.get("location")
    db.expire_all()
    assert db.get(Submission, submission_id).status in {"submitted", "confirmed"}


@pytest.mark.parametrize("case", ["path", "unknown_proposal", "invalid_price", "read_only", "forged_csrf", "storage_down"])
def test_web_package_upload_rejects_unsafe_or_unauthorized_input(upgraded_engine, db, client, tmp_path, monkeypatch, case):
    from test_release_lifecycle import prepared_lifecycle
    from test_web_ui import _make_user

    from govcon.collaboration.users import create_session
    from govcon.models import Proposal, Submission, User

    ids, _, _ = prepared_lifecycle(upgraded_engine, tmp_path / "source")
    opportunity_id, actor_id, proposal_id, submission_id = ids
    monkeypatch.setattr(client.app.state.settings, "data_dir", tmp_path / "uploads")
    actor = db.get(User, actor_id)
    if case == "read_only":
        actor, _ = _make_user(db, f"upload-reader-{opportunity_id}@regression.test", "read_only")
    client.cookies.set("govcon_session", create_session(db, actor))
    db.commit()
    proposal, submission = db.get(Proposal, proposal_id), db.get(Submission, submission_id)
    old_hash, old_version = submission.package_manifest_hash, submission.version
    fields = {"expected_proposal_version": str(proposal.version), "expected_submission_version": str(submission.version),
              "proposal_filename": "../outside.docx" if case == "path" else "proposal.docx", "pricing_rows": "0001,NaN,100" if case == "invalid_price" else ""}
    if case == "unknown_proposal":
        fields["proposal_filename"] = "unuploaded.docx"
    if case == "storage_down":
        from pathlib import Path

        def storage_failure(*args):
            raise OSError("synthetic storage outage")

        monkeypatch.setattr(Path, "write_bytes", storage_failure)
    headers = {"X-CSRF-Token": "0" * 64} if case == "forged_csrf" else {}
    response = client.post(f"/workspace/{opportunity_id}/submission/package/assemble", data=fields, headers=headers,
                           files=[("files", ("../outside.docx" if case == "path" else "proposal.docx", (tmp_path / "source" / "proposal.docx").read_bytes()))])
    assert response.status_code == 403 if case == "forged_csrf" else "error=" in response.headers["location"]
    db.expire_all()
    submission = db.get(Submission, submission_id)
    assert (submission.package_manifest_hash, submission.version) == (old_hash, old_version)
    assert not list((tmp_path / "uploads").rglob("*.docx"))


def test_package_upload_stream_limit_does_not_trust_content_length(client, monkeypatch):
    monkeypatch.setattr("govcon.web.security.PACKAGE_UPLOAD_LIMIT", 32)
    response = client.post("/workspace/1/submission/package/assemble", files={"files": ("test.docx", b"x" * 64)},
                           headers={"Content-Length": "1"})
    assert response.status_code == 413


@pytest.mark.parametrize("case", ["missing", "malformed"])
def test_required_shared_prompt_fragment_fails_closed(db, tmp_path, case):
    import shutil

    from govcon.ai.structured import StructuredCallError, resolve_prompt
    from govcon.config import Settings
    from govcon.prompting.registry import (
        PromptRegistryDenied,
        load_prompt,
        sync_prompts,
    )
    from govcon.prompting.renderer import PromptRenderError, render_system_prompt

    root = tmp_path / "prompts"
    shutil.copytree(Settings(_env_file=None).resolved_prompt_root(), root)
    configured = Settings(_env_file=None, prompt_root=root, prompt_require_behavioral_evaluation=False)
    sync_prompts(db, root, settings=configured)
    db.commit()
    asset = resolve_prompt(db, "solicitation_analysis", configured)
    assert render_system_prompt(asset, root)
    fragment = root / "shared" / "source_security_rules_v1.md"
    if case == "missing":
        fragment.unlink()
    else:
        fragment.write_text("malformed prompt without front matter", encoding="utf-8")
    with pytest.raises(PromptRegistryDenied, match="include"):
        load_prompt(db, "solicitation_analysis", prompt_root=root)
    with pytest.raises(StructuredCallError, match="include"):
        resolve_prompt(db, "solicitation_analysis", configured)
    with pytest.raises(PromptRenderError, match="include"):
        render_system_prompt(asset, root)


def test_web_no_bid_records_one_learning_outcome_and_reopen_removes_current_count(db, client):
    from sqlalchemy import func, select
    from test_proposal_generation_task import approved_bid

    from govcon.collaboration.review_sessions import finalize_approval
    from govcon.learning.analytics import outcome_analytics
    from govcon.models import OutcomeFeedback

    opp, pursuit, review, actor = approved_bid(db, client, approve=False)
    before = outcome_analytics(db).total_no_bid
    db.rollback()
    assert 'name="no_bid_reason"' in client.get(f"/workspace/{opp.id}?tab=review").text
    fields = {"decision": "no_bid", "expected_version": review.version,
              "no_bid_category": "margin", "no_bid_reason": "Verified supplier cost leaves insufficient margin."}
    response = client.post(f"/workspace/{opp.id}/approve", data=fields)
    assert "error=" not in response.headers["location"]
    db.expire_all()
    feedback = db.scalar(select(OutcomeFeedback).where(OutcomeFeedback.opportunity_id == opp.id))
    assert feedback and feedback.no_bid_category == "margin" and feedback.no_bid_reason == fields["no_bid_reason"]
    assert outcome_analytics(db).total_no_bid == before + 1
    assert db.get(type(pursuit), pursuit.id).outcome_at
    response = client.post(f"/workspace/{opp.id}/approve", data=fields)
    assert "error=" in response.headers["location"]
    assert db.scalar(select(func.count()).select_from(OutcomeFeedback).where(OutcomeFeedback.opportunity_id == opp.id)) == 1
    db.expire_all()
    review = db.get(type(review), review.id)
    finalize_approval(db, opportunity_id=opp.id, actor=db.get(type(actor), actor.id), action="return_for_review",
                      expected_version=review.version)
    db.commit()
    assert outcome_analytics(db).total_no_bid == before
    assert db.get(OutcomeFeedback, feedback.id).no_bid_reason == fields["no_bid_reason"]


@pytest.mark.parametrize("status,valid,expected", [
    ("unknown", False, "blocked"), ("unreviewed", False, "blocked"), ("needs_review", False, "blocked"),
    ("stale", False, "blocked"), ("missing", False, "blocked"), ("satisfied", False, "blocked"),
    ("satisfied", True, "ready"), ("not_applicable", False, "ready"),
])
def test_final_checklist_does_not_call_unresolved_mandatory_requirements_satisfied(db, status, valid, expected):
    from govcon.models import Requirement
    from govcon.submissions.checklist import generate_final_checklist

    opportunity = new_opportunity(db)
    db.add(Requirement(opportunity_id=opportunity.id, requirement_text="Synthetic mandatory requirement",
                       mandatory=True, status=status, validation={"methods": ["human_review"]} if valid else {}))
    db.commit()
    checklist = generate_final_checklist(db, opportunity_id=opportunity.id)
    item = next(item for item in checklist["items"] if item["label"] == "Mandatory requirements")
    assert item["status"] == expected
    if expected == "blocked":
        assert "not satisfied" in item["detail"] and checklist["mandatory_unresolved"] == 1
        assert checklist["mandatory_satisfied"] == 0


@pytest.mark.parametrize("tab", ["requirements", "compliance"])
def test_workspace_requirements_over_200_are_counted_and_reachable(db, client, tab):
    from uuid import uuid4

    from test_web_ui import _make_user

    from govcon.compliance.metrics import record_matrix_run
    from govcon.models import Requirement

    _, token = _make_user(db, uuid4().hex + "@regression.test", "owner")
    client.cookies.set("govcon_session", token)
    opportunity = new_opportunity(db)
    db.add_all([Requirement(opportunity_id=opportunity.id, requirement_text=f"REQUIREMENT-{index:04d}-PAGING",
                            mandatory=True, status="unknown", severity="high") for index in range(205)])
    db.flush()
    record_matrix_run(db, opportunity.id)
    db.commit()
    first = client.get(f"/workspace/{opportunity.id}?tab={tab}")
    assert "205 total" in first.text and "REQUIREMENT-0204-PAGING" not in first.text
    assert "requirements_page=2" in first.text
    second = client.get(f"/workspace/{opportunity.id}?tab={tab}&requirements_page=2")
    assert "REQUIREMENT-0204-PAGING" in second.text and "REQUIREMENT-0000-PAGING" not in second.text
    assert "requirements_page=1" in second.text
    assert client.get(f"/workspace/{opportunity.id}?tab={tab}&requirements_page=0").status_code == 422
    assert "REQUIREMENT-0204-PAGING" in client.get(f"/workspace/{opportunity.id}?tab={tab}&requirements_page=99").text


@pytest.mark.parametrize("score,provider,label", [(0.35, "rules", "Rules engine"), (0, "jev", "AI"), (0.35, None, "Engine not recorded")])
def test_overview_decision_matches_percentage_lists_and_provider(db, client, score, provider, label):
    from decimal import Decimal
    from uuid import uuid4

    from test_web_ui import _make_user

    from govcon.ai.analysis_types import AnalysisType
    from govcon.models import AIAnalysis, BidDecision

    _, token = _make_user(db, uuid4().hex + "@regression.test", "owner")
    client.cookies.set("govcon_session", token)
    opportunity = new_opportunity(db)
    db.add(BidDecision(opportunity_id=opportunity.id, recommendation="review", recommendation_score=Decimal(str(score)),
                       strengths={"items": ["Verified supplier exists"]}, risks={"items": ["Lead time unknown"]}))
    if provider:
        db.add(AIAnalysis(opportunity_id=opportunity.id, analysis_type=AnalysisType.DECISION_PACKAGE,
                          output_json={"bundle_runs": [{"provider": provider}]}))
    db.commit()
    for tab in ("overview", "ai_decision"):
        html = client.get(f"/workspace/{opportunity.id}?tab={tab}").text
        assert ("35%" if score else "0%") in html and label in html
        assert "<li>Verified supplier exists</li>" in html and "<li>Lead time unknown</li>" in html
        assert '&quot;items&quot;' not in html and "AI Bid Decision" not in html


@pytest.mark.parametrize("fields", [
    {"min_value": "abc"}, {"min_value": "200", "max_value": "100"}, {"min_deadline_days": "tomorrow"},
    {"min_value": "NaN"}, {"max_value": "Infinity"}, {"min_value": "-1"}, {"min_deadline_days": "2147483648"},
])
def test_watchlist_invalid_filters_preserve_saved_values_and_form(db, client, fields):
    from decimal import Decimal
    from uuid import uuid4

    from test_web_ui import _make_user

    from govcon.models import Watchlist

    _, token = _make_user(db, uuid4().hex + "@regression.test", "owner")
    client.cookies.set("govcon_session", token)
    name = "Validation " + uuid4().hex
    watchlist = Watchlist(name=name, min_value=Decimal("10"), max_value=Decimal("100"), min_deadline_days=3)
    db.add(watchlist)
    db.commit()
    response = client.post(f"/watchlists/{watchlist.id}/edit", data={"name": name, "keywords": "retained keyword", **fields})
    assert response.status_code == 422 and "retained keyword" in response.text
    assert f'/watchlists/{watchlist.id}/edit' in response.text
    db.expire_all()
    assert (watchlist.min_value, watchlist.max_value, watchlist.min_deadline_days) == (Decimal("10"), Decimal("100"), 3)


def test_watchlist_zero_filters_are_valid_and_displayed(db, client):
    from uuid import uuid4

    from sqlalchemy import select
    from test_web_ui import _make_user

    from govcon.models import Watchlist

    _, token = _make_user(db, uuid4().hex + "@regression.test", "owner")
    client.cookies.set("govcon_session", token)
    name = "Zero filters " + uuid4().hex
    response = client.post("/watchlists/new", data={"name": name, "min_value": "0", "max_value": "0", "min_deadline_days": "0"})
    assert response.status_code == 303
    watchlist = db.scalar(select(Watchlist).where(Watchlist.name == name))
    assert (watchlist.min_value, watchlist.max_value, watchlist.min_deadline_days) == (0, 0, 0)
    html = client.get(f"/watchlists/{watchlist.id}/edit").text
    assert 'name="min_deadline_days" value="0"' in html


def test_notification_acknowledgement_rechecks_revoked_account(db, client, monkeypatch):
    from uuid import uuid4

    from test_web_ui import _make_user

    from govcon.models import Notification, User
    from govcon.web.routes import accounts as routes

    actor, token = _make_user(db, uuid4().hex + "@regression.test", "owner")
    client.cookies.set("govcon_session", token)
    notification = Notification(user_id=actor.id, notification_type="review_assigned", payload={})
    db.add(notification)
    db.commit()
    original = routes._require_login

    def revoke_after_login(request):
        authenticated = original(request)
        db.get(User, authenticated.id).is_active = False
        db.commit()
        return authenticated

    monkeypatch.setattr(routes, "_require_login", revoke_after_login)
    response = client.post(f"/notifications/{notification.id}/acknowledge")
    assert response.status_code == 303 and response.headers["location"].startswith("/login")
    db.expire_all()
    assert db.get(Notification, notification.id).acknowledged_at is None
