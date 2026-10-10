"""Workspace compliance section: every requirement, its source, fact, checks and status.

Covers the read model (categories, Met/Missing/Needs review/Not applicable,
company-fact evidence), the incomplete-run banner for sparse or partial AI
output, and the web override, which needs ``override_compliance`` and is
audited.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_web_ui import _make_user

from govcon.compliance.extractor import PassOutcome
from govcon.compliance.matrix import record_run
from govcon.compliance.metrics import record_matrix_run
from govcon.compliance.pipeline import mark_sparse_ai_passes
from govcon.compliance.records import Candidate
from govcon.compliance.view import category_for, company_fact_evidence, compliance_view
from govcon.models import (
    AuditEvent,
    ComplianceRun,
    Opportunity,
    Requirement,
    StoredFile,
)


def _opp(db, **values) -> Opportunity:
    data = {"source": "sam", "source_id": uuid4().hex, "title": f"Compliance view {uuid4().hex[:6]}", "status": "open",
            "response_deadline": datetime.now(UTC) + timedelta(days=30), "raw": {}, "links": {}}
    data.update(values)
    opp = Opportunity(**data)
    db.add(opp)
    db.flush()
    return opp


def _file(db, opp, filename: str) -> StoredFile:
    row = StoredFile(classification="PUBLIC", source_origin="synthetic_test_fixture", opportunity_id=opp.id,
                     filename=filename, url=f"https://example.test/{uuid4().hex}/{filename}", mime_type="text/plain",
                     sha256=hashlib.sha256(uuid4().bytes).hexdigest(), extracted_text="synthetic",
                     extraction_status="success", downloaded_at=datetime.now(UTC))
    db.add(row)
    db.flush()
    return row


def _req(db, opp, text: str, **values) -> Requirement:
    data = {"opportunity_id": opp.id, "requirement_text": text, "mandatory": True, "severity": "high", "status": "unknown"}
    data.update(values)
    row = Requirement(**data)
    db.add(row)
    db.flush()
    return row


def _login(db, client, role: str):
    user, token = _make_user(db, f"{role}-{uuid4().hex}@compliance.test", role)
    client.cookies.set("govcon_session", token)
    return user


def _stat(text: str, label: str) -> int:
    match = re.search(r'<div class="label">' + re.escape(label) + r'</div>\s*<div class="value[^"]*">(\d+)</div>', text)
    assert match, f"stat {label!r} not rendered"
    return int(match.group(1))


SYNTHETIC_FACTS = {"sam_registration_status": "Active", "sam_expiration_date": "2099-01-01",
                   "socioeconomic": {"small_business": True}}


@pytest.fixture()
def facts_file(tmp_path, monkeypatch):
    """Requested before ``client``: the app binds its settings when it is created."""
    from govcon.config import get_settings

    path = tmp_path / "company_facts.json"
    path.write_text(json.dumps(SYNTHETIC_FACTS), encoding="utf-8")
    monkeypatch.setenv("COMPANY_FACTS_PATH", str(path))
    get_settings.cache_clear()
    yield path
    get_settings.cache_clear()


# ── read model ──


@pytest.mark.parametrize("values,expected", [
    ({"requirement_type": "set_aside", "requirement_text": "Total small business set-aside"}, "set_aside"),
    ({"requirement_type": "administrative", "requirement_text": "Offeror must be registered in SAM at award"}, "registration"),
    ({"requirement_type": "technical", "requirement_text": "FAR 52.211-14 rated order applies"}, "clauses"),
    ({"requirement_type": "certification", "requirement_text": "Provide ISO 9001 certificate"}, "certifications"),
    ({"requirement_type": "delivery", "requirement_text": "Package per MIL-STD-2073-1"}, "packaging"),
    ({"requirement_type": "delivery", "requirement_text": "Deliver within 30 days ARO"}, "delivery"),
    ({"requirement_type": "past_performance", "requirement_text": "Three recent contracts"}, "section_m"),
    ({"requirement_type": "technical", "requirement_text": "Offers are evaluated on technical merit", "source_section": "SECTION M"}, "section_m"),
    ({"requirement_type": "page_limit", "requirement_text": "Technical volume limited to 10 pages"}, "section_l"),
    ({"requirement_type": "technical", "requirement_text": "Gloves are nitrile, powder free"}, "technical"),
    ({"requirement_type": "pricing", "requirement_text": "Firm fixed price per unit"}, "pricing"),
    ({"requirement_type": None, "requirement_text": "Something else entirely"}, "other"),
])
def test_each_requirement_lands_in_one_category(values, expected):
    assert category_for(Requirement(**values)) == expected


def test_company_fact_evidence_names_the_missing_fact():
    opp = Opportunity(set_aside_code="SBA")
    registration = Requirement(requirement_text="Registered in SAM")
    assert company_fact_evidence("registration", registration, opp, {}).text == "missing company fact: SAM registration status"
    active = company_fact_evidence("registration", registration, opp,
                                   {"sam_registration_status": "Active", "sam_expiration_date": "2027-04-01", "uei": "SYNTH12345AB"})
    assert active.state == "present" and "expires 2027-04-01" in active.text and "SYNTH12345AB" in active.text
    set_aside = Requirement(requirement_text="Small business set-aside", requirement_type="set_aside")
    assert company_fact_evidence("set_aside", set_aside, opp, {}).text == "missing company fact: small_business status (set-aside SBA)"
    assert company_fact_evidence("set_aside", set_aside, opp, {"socioeconomic": {"small_business": False}}).state == "contradicts"
    assert company_fact_evidence("set_aside", set_aside, Opportunity(set_aside_code=None), {}).state == "not_applicable"
    cert = Requirement(requirement_text="Provide a current ISO 9001 certificate", requirement_type="certification")
    assert company_fact_evidence("certifications", cert, opp, {"certifications": ["ISO 9001"]}).state == "present"
    unmatched = company_fact_evidence("certifications", cert, opp, {"certifications": ["AS9100"]})
    assert unmatched.missing and "AS9100" in unmatched.text
    assert company_fact_evidence("packaging", cert, opp, {}).state == "not_applicable"


def test_view_maps_every_status_and_hides_superseded(db):
    opp = _opp(db)
    for status in ("satisfied", "missing", "unknown", "needs_review", "unreviewed", "stale", "not_applicable"):
        _req(db, opp, f"Synthetic {status}", status=status)
    replaced = _req(db, opp, "Synthetic old", status="superseded")
    view = compliance_view(db, opp.id, facts={})
    labels = {row["requirement"]: row["status_label"] for row in view.rows}
    assert labels == {
        "Synthetic satisfied": "Met", "Synthetic missing": "Missing", "Synthetic not_applicable": "Not applicable",
        "Synthetic unknown": "Needs review", "Synthetic needs_review": "Needs review",
        "Synthetic unreviewed": "Needs review", "Synthetic stale": "Needs review",
    }
    assert replaced.id not in {row["requirement_id"] for row in view.rows} and view.superseded == 1
    assert view.mandatory == {"total": 7, "Met": 1, "Missing": 1, "Needs review": 4, "Not applicable": 1}
    stale = next(row for row in view.rows if row["requirement"] == "Synthetic stale")
    assert stale["status_reason_text"] == "Changed by an amendment since it was last validated"
    db.rollback()


# ── page ──


def test_page_shows_source_fact_checks_and_ai_side_by_side(db, client):
    _login(db, client, "reviewer")
    opp = _opp(db, set_aside_code="SBA")
    rfq = _file(db, opp, "Attachment_02_RFQ.pdf")
    _req(db, opp, "Offeror shall be registered in SAM before award", requirement_type="administrative",
         source_file_id=rfq.id, source_page=7, source_section="SECTION K",
         source_quote="Offerors must be registered in the System for Award Management (SAM) prior to award.",
         status="missing", status_reason="deterministic validator failed: sam_registration_known",
         validation={
             "deterministic": [{"validator": "sam_registration_known", "status": "fail", "reason": "SAM registration status is expired"}],
             "ai": {"status": "SATISFIED", "reason": "Synthetic AI says registered", "confidence": 0.91,
                    "provider": "deepseek", "model": "deepseek-flash"},
             "methods": ["deterministic", "ai"],
         })
    _req(db, opp, "Small business set-aside applies", requirement_type="set_aside", status="unknown")
    record_matrix_run(db, opp.id)
    db.commit()

    page = client.get(f"/workspace/{opp.id}?tab=compliance")
    assert page.status_code == 200
    text = page.text
    assert "Attachment_02_RFQ.pdf" in text and "page 7" in text and "SECTION K" in text
    assert "Offerors must be registered in the System for Award Management (SAM) prior to award." in text
    assert "missing company fact: SAM registration status" in text
    assert "missing company fact: small_business status (set-aside SBA)" in text
    assert "sam_registration_known" in text and "SAM registration status is expired" in text
    assert "Primary: SATISFIED" in text and "91% confidence" in text and "deepseek" in text
    assert "Registration (SAM, UEI, CAGE)" in text and "Set-aside and socioeconomic status" in text
    assert _stat(text, "Missing") == 1 and _stat(text, "Needs review") == 1 and _stat(text, "Missing company facts") == 2
    assert 'data-status="unknown">Needs review<' in text and ">unknown<" not in text
    assert "/requirements/" not in text, "a reviewer has no override_compliance permission, so no override form"


def test_page_reads_company_facts_when_they_exist(db, facts_file, client):
    _login(db, client, "reviewer")
    opp = _opp(db, set_aside_code="SBA")
    _req(db, opp, "Small business set-aside applies", requirement_type="set_aside")
    record_matrix_run(db, opp.id)
    db.commit()
    text = client.get(f"/workspace/{opp.id}?tab=compliance").text
    assert "Company facts assert small_business (set-aside SBA)" in text
    assert _stat(text, "Missing company facts") == 0


def test_incomplete_run_is_never_shown_as_complete(db, client):
    _login(db, client, "reviewer")
    opp = _opp(db)
    _req(db, opp, "Synthetic requirement")
    warning = {"code": "pass_a_sparse", "severity": "high", "message": "Extraction pass A returned 0 requirement(s) where the deterministic scanner found 6"}
    record_matrix_run(db, opp.id, status="incomplete", warnings=[warning])
    db.commit()
    text = client.get(f"/workspace/{opp.id}?tab=compliance").text
    assert 'data-run-state="incomplete"' in text and "Run incomplete." in text and "Run complete." not in text
    assert warning["message"] in text
    assert "No AI validation result is stored" in text, "a run without AI results must not carry an AI label"

    complete = _opp(db)
    _req(db, complete, "Synthetic requirement")
    record_matrix_run(db, complete.id, status="complete")
    db.commit()
    text = client.get(f"/workspace/{complete.id}?tab=compliance").text
    assert 'data-run-state="complete"' in text and "not that the company is compliant" in text


# ── sparse and partial AI output ──


def _candidate(n: int, label: str) -> Candidate:
    return Candidate(candidate_id=f"{label}-{n}", pass_label=label, requirement_text=f"Synthetic requirement {n}",
                     requirement_type="technical", mandatory=True, severity="high", source_file_id=None,
                     source_page=1, supporting_quote=f"quote {n}")


def _pass_run(db, opp, label: str) -> ComplianceRun:
    return record_run(db, opportunity_id=opp.id, run_type=f"extraction_pass_{label.lower()}", run_version="test", output={})


@pytest.mark.parametrize("scanner,a_count,b_count,sparse", [
    (6, 0, 6, {"A"}),
    (6, 2, 3, {"A"}),
    (6, 3, 3, set()),
    (2, 0, 1, {"A"}),
    (2, 1, 1, set()),
    (0, 0, 0, set()),
])
def test_sparse_ai_passes_are_marked_incomplete(db, scanner, a_count, b_count, sparse):
    opp = _opp(db)
    outcomes = [PassOutcome("D", [_candidate(i, "D") for i in range(scanner)], "complete", provider="deterministic")]
    for label, count in (("A", a_count), ("B", b_count)):
        run = _pass_run(db, opp, label)
        outcomes.append(PassOutcome(label, [_candidate(i, label) for i in range(count)], "complete", run.id, provider="fake"))
    warnings = mark_sparse_ai_passes(db, outcomes)
    marked = {o.pass_label for o in outcomes if o.status == "incomplete"}
    assert marked == sparse
    assert {w["code"] for w in warnings} == {f"pass_{label.lower()}_sparse" for label in sparse}
    for outcome in outcomes[1:]:
        run = db.get(ComplianceRun, outcome.run_id)
        assert (run.status == "incomplete") == (outcome.pass_label in sparse)
    db.rollback()


def test_partial_ai_validation_makes_the_run_incomplete(db, monkeypatch):
    from govcon.compliance import validator

    opp = _opp(db)
    passed = {"deterministic": [{"validator": "deadline_not_passed", "status": "pass", "reason": "synthetic"}]}
    answered = _req(db, opp, "Synthetic answered", validation=passed)
    skipped = _req(db, opp, "Synthetic skipped", validation=passed)

    def partial(session, opportunity_id, requirements, evidence, *, label, **_):
        if label != "ai_validation":
            return {}
        return {answered.id: {"status": "NOT_SATISFIED", "reason": "synthetic", "confidence": 0.9, "evidence_ids": []}}

    monkeypatch.setattr(validator, "_ai_validate", partial)
    result = validator.run_validation(db, opp.id, use_ai=True)
    assert result["ai_complete"] is False
    warning = next(w for w in result["warnings"] if w["code"] == "ai_validation_partial")
    assert str(skipped.id) in warning["message"] and "1 of 2" in warning["message"]
    assert validator.run_validation(db, opp.id, use_ai=False)["ai_complete"] is True
    db.rollback()


def test_pipeline_reports_incomplete_when_a_pass_is_sparse(db, monkeypatch):
    from govcon.compliance import pipeline

    opp = _opp(db)
    db.add(StoredFile(classification="PUBLIC", source_origin="synthetic_test_fixture", opportunity_id=opp.id,
                      filename="RFQ.txt", url="https://example.test/rfq.txt", mime_type="text/plain",
                      sha256=hashlib.sha256(uuid4().bytes).hexdigest(), downloaded_at=datetime.now(UTC),
                      extraction_status="success",
                      extracted_text="\n".join(f"{n}. The contractor shall provide item {n} with certificate of conformance." for n in range(1, 9))))
    db.flush()

    def empty_pass(session, opportunity, inventory, label, *, settings=None):
        run = _pass_run(session, opportunity, label)
        return PassOutcome(label, [], "complete", run.id, provider="fake", model=f"fake-{label}")

    monkeypatch.setattr(pipeline, "run_ai_pass", empty_pass)
    monkeypatch.setattr(pipeline, "run_validation", lambda *a, **k: {"run_id": None, "changed": 0, "warnings": [], "decisions": {}, "ai_complete": True})
    monkeypatch.setattr(pipeline, "run_red_team", lambda *a, **k: {"run_id": None, "finding_ids": [], "ai": None})
    monkeypatch.setattr(pipeline, "run_conflict_scan", lambda *a, **k: {"run_id": None, "applied": {"superseded": []}})
    result = pipeline.run_compliance_pipeline(db, opp.id, use_ai=True, company_facts={})
    assert result["extraction"]["passes"]["D"]["candidates"] > 0
    assert result["status"] == "incomplete"
    assert {"pass_a_sparse", "pass_b_sparse"} <= {w["code"] for w in result["warnings"]}
    db.rollback()


# ── override ──


def _override(client, opp, req, **form):
    data = {"status": "satisfied", "reason": "Verified the SAM record by phone", "expected_version": req.version}
    data.update(form)
    return client.post(f"/workspace/{opp.id}/requirements/{req.id}/override", data=data)


def test_override_needs_permission_reason_and_acknowledgement_and_is_audited(db, client):
    opp = _opp(db)
    failing = _req(db, opp, "Registered in SAM", requirement_type="administrative", validation={
        "deterministic": [{"validator": "sam_registration_known", "status": "fail", "reason": "expired"}]})
    other = _opp(db)
    db.commit()

    _login(db, client, "reviewer")
    denied = _override(client, opp, failing)
    assert denied.status_code == 303 and "error=1" in denied.headers["location"]

    owner = _login(db, client, "owner")
    page = client.get(f"/workspace/{opp.id}?tab=compliance").text
    assert f'action="/workspace/{opp.id}/requirements/{failing.id}/override"' in page
    assert 'name="acknowledge_deterministic_failure"' in page
    for form in ({"reason": "too short"}, {}, {"expected_version": failing.version + 5}):
        response = _override(client, opp, failing, **form)
        assert "error=1" in response.headers["location"], form
    assert "error=1" in client.post(f"/workspace/{other.id}/requirements/{failing.id}/override", data={
        "status": "satisfied", "reason": "Verified the SAM record by phone", "expected_version": failing.version,
        "acknowledge_deterministic_failure": "yes"}).headers["location"]
    db.expire_all()
    assert db.get(Requirement, failing.id).status == "unknown"
    assert db.scalar(select(AuditEvent).where(AuditEvent.entity_type == "requirements", AuditEvent.entity_id == failing.id)) is None

    accepted = _override(client, opp, failing, acknowledge_deterministic_failure="yes")
    assert "notice=1" in accepted.headers["location"]
    db.expire_all()
    row = db.get(Requirement, failing.id)
    assert row.status == "satisfied" and row.verified_by_human and row.validation["override"]["user_id"] == owner.id
    event = db.scalar(select(AuditEvent).where(AuditEvent.entity_type == "requirements", AuditEvent.entity_id == failing.id))
    assert event.action_type == "compliance_requirement_override" and event.user_id == owner.id
    assert event.new_value["deterministic_failures_overridden"] == ["sam_registration_known"]
    page = client.get(f"/workspace/{opp.id}?tab=compliance").text
    assert f"Human override to satisfied by user #{owner.id}" in page
