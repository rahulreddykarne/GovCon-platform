"""Compliance section: every requirement grouped, explained, and never shown complete when it is not."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from govcon.compliance.pipeline import sparse_pass_warnings
from govcon.compliance.view import analysis_state, category_for, company_fact_evidence
from govcon.models import AuditEvent, Opportunity, Requirement, StoredFile


def _req(**values) -> Requirement:
    base = {"requirement_text": "The offeror shall comply.", "requirement_type": "other", "status": "unknown"}
    base.update(values)
    return Requirement(**base)


@pytest.mark.parametrize("values,expected", [
    ({"requirement_text": "FAR 52.204-7 System for Award Management applies."}, "clauses"),
    ({"requirement_type": "certification", "requirement_text": "Provide ISO 9001 certificate."}, "certifications"),
    ({"requirement_type": "administrative", "requirement_text": "Offerors must be registered in SAM with an active UEI."},
     "registration"),
    ({"requirement_type": "set_aside", "requirement_text": "Total small business set-aside."}, "set_aside"),
    ({"requirement_type": "delivery", "requirement_text": "Mark each package per MIL-STD-129."}, "packaging"),
    ({"requirement_type": "delivery", "requirement_text": "Deliver within 30 days ARO."}, "delivery"),
    ({"requirement_type": "technical", "source_section": "Section M.2", "requirement_text": "Technical approach."},
     "section_m"),
    ({"requirement_type": "page_limit", "requirement_text": "Volume I shall not exceed 10 pages."}, "section_l"),
    ({"requirement_type": "pricing", "requirement_text": "Price every CLIN."}, "pricing"),
    ({"requirement_type": "past_performance", "requirement_text": "Provide three references."}, "past_performance"),
    ({"requirement_type": "other", "requirement_text": "Contractor personnel shall be courteous."}, "other"),
])
def test_requirements_are_grouped_by_what_they_ask(values, expected):
    assert category_for(_req(**values)) == expected


def test_missing_company_facts_are_named_not_guessed():
    set_aside = _req(requirement_type="set_aside", validation={"deterministic": [
        {"validator": "set_aside_matches", "status": "unknown", "reason": "company facts do not establish small_business status",
         "evidence": {"code": "SBA", "status": "small_business"}}]})
    facts = company_fact_evidence(set_aside, "set_aside", {})
    assert [(f.state, f.text) for f in facts] == [("missing", "missing company fact: small_business status")]

    cert = _req(requirement_type="certification", requirement_text="Provide ISO 9001 certificate.")
    assert company_fact_evidence(cert, "certifications", {})[0].text == "missing company fact: certifications held"
    held = company_fact_evidence(cert, "certifications", {"certifications": ["ISO 9001"]})
    assert held[0].state == "present" and "ISO 9001" in held[0].text

    sam = _req(requirement_text="Registered in SAM.", validation={"deterministic": [
        {"validator": "sam_registration_known", "status": "pass", "reason": "SAM registration active"}]})
    assert [(f.state, f.text) for f in company_fact_evidence(sam, "registration", {})] == [
        ("present", "SAM registration active")]


def test_sparse_or_empty_ai_passes_are_incomplete():
    passes = [SimpleNamespace(pass_label="A", status="complete", candidates=[]),
              SimpleNamespace(pass_label="B", status="complete", candidates=[object()])]
    codes = [w["code"] for w in sparse_pass_warnings(40, passes)]
    assert codes == ["pass_a_no_requirements", "pass_b_sparse"]
    assert sparse_pass_warnings(40, [SimpleNamespace(pass_label="A", status="complete", candidates=[object()] * 5)]) == []
    # A failed pass is reported by the extractor itself; no duplicate warning.
    assert sparse_pass_warnings(40, [SimpleNamespace(pass_label="A", status="failed", candidates=[])]) == []


def test_analysis_state_never_reports_partial_output_as_complete():
    assert analysis_state(None, None, 0)["state"] == "not_run"
    done = SimpleNamespace(status="complete", warnings=None)
    assert analysis_state(done, None, 3)["state"] == "complete"
    assert analysis_state(done, None, 0)["state"] == "incomplete"
    empty_pass = SimpleNamespace(output_json={"passes": {"A": {"status": "complete", "candidates": 0},
                                                         "B": {"status": "complete", "candidates": 4},
                                                         "D": {"status": "complete", "candidates": 0}}})
    state = analysis_state(done, empty_pass, 4)
    assert state["state"] == "incomplete" and state["reasons"] == ["Extraction pass A returned no requirements."]
    partial = SimpleNamespace(status="incomplete", warnings=[
        {"code": "context_truncated", "severity": "high", "message": "Pass B could not read pricing.pdf pages 4"}])
    state = analysis_state(partial, None, 9)
    assert state["state"] == "incomplete" and "Pass B could not read pricing.pdf pages 4" in state["reasons"]


# ── page and override route ──────────────────────────────────────────────────

def _login(db, client, role):
    from test_web_ui import _make_user

    user, token = _make_user(db, f"cv-{role}-{uuid4().hex[:8]}@example.test", role)
    db.commit()
    client.cookies.set("govcon_session", token)
    return user


def _opportunity_with_requirements(db):
    from govcon.compliance.matrix import record_run
    from govcon.compliance.metrics import record_matrix_run

    opp = Opportunity(source="sam", source_id=f"cv-{uuid4().hex}", title="Compliance view fixture", status="open",
                      set_aside_code="SBA", raw={}, links={})
    db.add(opp)
    db.flush()
    stored = StoredFile(opportunity_id=opp.id, filename="solicitation.pdf", classification="PUBLIC")
    db.add(stored)
    db.flush()
    set_aside = Requirement(
        opportunity_id=opp.id, requirement_type="set_aside", mandatory=True, severity="critical", status="unknown",
        requirement_text="This acquisition is a total small business set-aside.", source_file_id=stored.id, source_page=3,
        source_section="Section K", source_quote="100% set aside for small business",
        validation={"deterministic": [{"validator": "set_aside_matches", "status": "unknown",
                                       "reason": "company facts do not establish small_business status",
                                       "evidence": {"code": "SBA", "status": "small_business"}}]},
    )
    deadline = Requirement(
        opportunity_id=opp.id, requirement_type="submission", mandatory=True, status="missing",
        requirement_text="Quotes are due by 2026-11-01 14:00 ET.", source_file_id=stored.id, source_page=1,
        source_quote="Quotes are due by 2026-11-01",
        validation={"deterministic": [{"validator": "deadline_not_passed", "status": "fail", "reason": "deadline passed"}],
                    "ai": {"status": "satisfied", "reason": "the quote says it will be on time", "confidence": 0.9,
                           "provider": "deepseek", "model": "deepseek-flash"}},
    )
    db.add_all([set_aside, deadline])
    db.flush()
    record_run(db, opportunity_id=opp.id, run_type="requirement_reconciliation", run_version="test",
               output={"passes": {"A": {"status": "complete", "candidates": 2}, "B": {"status": "failed", "candidates": 0}}},
               status="incomplete")
    record_matrix_run(db, opp.id, status="incomplete", warnings=[
        {"code": "pass_b_provider_error", "severity": "high", "message": "Extraction pass B produced no usable output"}])
    db.commit()
    return opp, set_aside, deadline


def test_compliance_tab_lists_each_requirement_with_source_facts_checks_and_ai(db, client):
    _login(db, client, "owner")
    opp, set_aside, deadline = _opportunity_with_requirements(db)
    page = client.get(f"/workspace/{opp.id}?tab=compliance")
    assert page.status_code == 200
    text = page.text
    assert 'data-analysis-state="incomplete"' in text and "Extraction incomplete." in text
    assert "Extraction pass B produced no usable output" in text and "Extraction pass B is failed." in text
    assert "Set-aside and eligibility" in text and "Section L: instructions to offerors" in text
    assert "100% set aside for small business" in text and "solicitation.pdf" in text and "page 3" in text
    assert "missing company fact: small_business status" in text
    assert "Set-aside eligibility" in text and "Deadline not passed" in text and "deadline passed" in text
    # The AI said satisfied; the failed deterministic check sits beside it and the row stays Missing.
    assert "the quote says it will be on time" in text and "deepseek · deepseek-flash" in text
    assert "A deterministic check failed; the AI result cannot clear it." in text
    assert f'id="req-{deadline.id}"' in text and "Save override" in text


def test_reviewer_cannot_override_and_nothing_changes(db, client):
    _login(db, client, "reviewer")
    opp, set_aside, _ = _opportunity_with_requirements(db)
    page = client.get(f"/workspace/{opp.id}?tab=compliance")
    assert "Save override" not in page.text
    response = client.post(f"/workspace/{opp.id}/requirements/{set_aside.id}/override",
                           data={"status": "satisfied", "reason": "We are a small business per SBA.",
                                 "expected_version": str(set_aside.version)})
    assert response.status_code == 303 and "error=1" in response.headers["location"]
    db.expire_all()
    assert db.get(Requirement, set_aside.id).status == "unknown"
    assert db.scalar(select(func.count()).select_from(AuditEvent).where(
        AuditEvent.action_type == "compliance_requirement_override", AuditEvent.opportunity_id == opp.id)) == 0


def test_owner_override_is_authorized_reasoned_and_logged(db, client):
    user = _login(db, client, "owner")
    opp, set_aside, deadline = _opportunity_with_requirements(db)
    url = f"/workspace/{opp.id}/requirements/{set_aside.id}/override"
    short = client.post(url, data={"status": "satisfied", "reason": "ok", "expected_version": str(set_aside.version)})
    assert "error=1" in short.headers["location"]

    ok = client.post(url, data={"status": "satisfied", "reason": "SBA profile confirms small business status.",
                                "expected_version": str(set_aside.version)})
    assert ok.status_code == 303 and "notice=1" in ok.headers["location"]
    db.expire_all()
    assert db.get(Requirement, set_aside.id).status == "satisfied"
    event = db.scalar(select(AuditEvent).where(AuditEvent.action_type == "compliance_requirement_override",
                                               AuditEvent.entity_id == set_aside.id))
    assert event is not None and event.user_id == user.id
    assert event.new_value["reason"] == "SBA profile confirms small business status."

    # A failed deterministic check must be acknowledged explicitly.
    refused = client.post(f"/workspace/{opp.id}/requirements/{deadline.id}/override",
                          data={"status": "satisfied", "reason": "Agency extended the deadline by email.",
                                "expected_version": str(deadline.version)})
    assert "error=1" in refused.headers["location"]
    db.expire_all()
    assert db.get(Requirement, deadline.id).status == "missing"

    page = client.get(f"/workspace/{opp.id}?tab=compliance").text
    assert "Override log" in page and "SBA profile confirms small business status." in page
    assert "Needs review → Met" in page


def test_override_targets_only_this_opportunity(db, client):
    _login(db, client, "owner")
    opp, set_aside, _ = _opportunity_with_requirements(db)
    other, _, _ = _opportunity_with_requirements(db)
    response = client.post(f"/workspace/{other.id}/requirements/{set_aside.id}/override",
                           data={"status": "satisfied", "reason": "Wrong opportunity on purpose.",
                                 "expected_version": str(set_aside.version)})
    assert "error=1" in response.headers["location"]
    db.expire_all()
    assert db.get(Requirement, set_aside.id).status == "unknown"
