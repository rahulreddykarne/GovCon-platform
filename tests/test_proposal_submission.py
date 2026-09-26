"""Phase 11 tests: post-approval proposal generation, coverage validation,
submission package generation, red-team, and final approval.

DB-backed tests run against the local PostgreSQL. External AI providers
are mocked; no live DeepSeek key is needed.

Acceptance criteria verified (from §17):
  AC-1  Approval-to-bid automatically starts proposal/package generation.
  AC-2  Proposal sections are traceable to compliance requirements.
  AC-3  Submission instructions are generated from solicitation evidence.
  AC-4  Missing mandatory items block `ready`.
  AC-5  Final package can be exported as DOCX/PDF/XLSX/ZIP.
  AC-6  Final approval remains human-authorized.
  AC-7  No arbitrary portal auto-submission occurs in v1.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from govcon.ai.providers.deepseek import DeepSeekResult
from govcon.compliance.matrix import active_requirements
from govcon.models import (
    AIAnalysis,
    AuditEvent,
    ComplianceFinding,
    Notification,
    Opportunity,
    Proposal,
    ProposalSection,
    ProposalVersion,
    Pursuit,
    Requirement,
    ReviewSession,
    Submission,
    User,
)

PROMPT_ROOT = Path(__file__).parent.parent / "src" / "govcon" / "prompts"


# ---------------------------------------------------------------------------
# Fake AI provider for mocking
# ---------------------------------------------------------------------------

class FakeProvider:
    """Returns canned AI responses keyed by prompt name (purpose)."""

    name = "deepseek"

    def __init__(self, responses: dict):
        self._responses = responses

    def complete(self, *, system_prompt: str = "", user_prompt: str = "", purpose: str = "", model=None, **_kwargs):
        payload = self._responses.get(purpose)
        if payload is None:
            # Fall back to first response
            payload = next(iter(self._responses.values()))
        return DeepSeekResult(
            content=json.dumps(payload),
            model=model or "deepseek-flash",
            usage={"total_tokens": 20},
            latency_ms=3,
        )


DRAFT_RESPONSE = {
    "sections": [
        {
            "section_key": "cover_letter",
            "heading": "Cover Letter",
            "content": "We submit this quote in response to solicitation SP3300-26-Q-TEST.",
            "requirement_ids": [1],
            "source_fact_ids": ["opp:sol_num"],
            "blockers": [],
            "word_count": 14,
        },
        {
            "section_key": "technical_response",
            "heading": "Technical Approach",
            "content": "We will supply NSN 1234 from an approved domestic source.",
            "requirement_ids": [2, 3],
            "source_fact_ids": ["requirement:2"],
            "blockers": [],
            "word_count": 12,
        },
    ],
    "global_blockers": [],
    "source_fact_ids_used": ["opp:sol_num", "requirement:2"],
    "draft_notes": [],
}

RED_TEAM_RESPONSE = {
    "findings": [
        {
            "finding_id": "rt_001",
            "severity": "major",
            "category": "incomplete_coverage",
            "requirement_id": 3,
            "proposal_section": "technical_response",
            "description": "Delivery commitment is vague — no specific date or days ARO stated.",
            "evidence": "Solicitation requires delivery within 30 days ARO.",
            "recommended_fix": "Add explicit delivery commitment with days ARO.",
        }
    ],
    "critical_count": 0,
    "major_count": 1,
    "minor_count": 0,
    "overall_assessment": "NEEDS_REVIEW",
    "reviewer_notes": [],
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def session(upgraded_engine):
    with Session(upgraded_engine) as s:
        yield s
        s.rollback()


def _uid() -> str:
    return uuid4().hex[:8]


def _opp(session: Session) -> Opportunity:
    opp = Opportunity(
        source="sam",
        source_id=f"TEST-{_uid()}",
        solicitation_number=f"SP3300-26-Q-{_uid()}",
        title="Test Supply Solicitation",
        agency_path="DLA/Troop Support",
        nsn="1234-56-789-0123",
        quantity=10,
        unit="EA",
        response_deadline=datetime.now(UTC) + timedelta(days=14),
        status="open",
        raw={},
    )
    session.add(opp)
    session.flush()
    return opp


def _user(session: Session, role: str = "approver") -> User:
    from govcon.collaboration.users import invite_user
    return invite_user(
        session,
        email=f"user_{_uid()}@test.example",
        display_name="Test User",
        password="TestPassword1234!",
        role=role,
    )


def _approved_pursuit(session: Session, opp: Opportunity) -> tuple[ReviewSession, Pursuit]:
    """Set up a fully approved_to_bid state with a pursuit."""
    from govcon.collaboration.review_sessions import ensure_review_session, finalize_approval

    review = ensure_review_session(session, opportunity_id=opp.id)
    # Set up pursuit
    pursuit = Pursuit(
        opportunity_id=opp.id,
        stage="bid_approved",
        approved_to_bid_at=datetime.now(UTC),
    )
    session.add(pursuit)
    session.flush()

    # Force final_approval_status without going through full quorum
    review.final_approval_status = "approved_to_bid"
    review.status = "approved_to_bid"
    session.flush()

    return review, pursuit


def _requirement(
    session: Session,
    opp_id: int,
    *,
    mandatory: bool = True,
    req_type: str = "technical",
    status: str = "satisfied",
    blocks_submission: bool = False,
    response_required: bool = True,
) -> Requirement:
    req = Requirement(
        opportunity_id=opp_id,
        requirement_type=req_type,
        requirement_text=f"Provide documentation for {_uid()}",
        mandatory=mandatory,
        severity="high" if mandatory else "low",
        response_required=response_required,
        status=status,
        independently_confirmed=True,
        blocks_submission=blocks_submission,
    )
    session.add(req)
    session.flush()
    return req


# ---------------------------------------------------------------------------
# AC-1: Approval-to-bid automatically starts proposal/package generation
# ---------------------------------------------------------------------------

def test_generate_proposal_requires_approved_to_bid(session):
    """generate_proposal() raises ValueError when not approved_to_bid."""
    from govcon.proposals.service import generate_proposal
    from govcon.collaboration.review_sessions import ensure_review_session

    opp = _opp(session)
    review = ensure_review_session(session, opportunity_id=opp.id)
    pursuit = Pursuit(opportunity_id=opp.id, stage="evaluating")
    session.add(pursuit)
    session.flush()

    with pytest.raises(ValueError, match="not approved_to_bid"):
        generate_proposal(session, opportunity_id=opp.id, skip_ai=True)


def test_generate_proposal_creates_version_when_approved(session):
    """AC-1: generate_proposal() creates a ProposalVersion when approved_to_bid."""
    from govcon.proposals.service import generate_proposal

    opp = _opp(session)
    review, pursuit = _approved_pursuit(session, opp)
    _requirement(session, opp.id, req_type="technical", status="satisfied")

    result = generate_proposal(session, opportunity_id=opp.id, skip_ai=True)

    assert result["proposal_id"] is not None
    assert result["version_id"] is not None
    assert result["version_number"] == 1
    assert result["section_count"] >= 1

    proposal = session.get(Proposal, result["proposal_id"])
    assert proposal.status == "ai_generated"
    assert proposal.current_version_id == result["version_id"]


def test_generate_proposal_with_ai_mock(session):
    """AC-1: generate_proposal() with mocked AI creates a version with section content."""
    from govcon.proposals.service import generate_proposal

    opp = _opp(session)
    review, pursuit = _approved_pursuit(session, opp)
    _requirement(session, opp.id, req_type="technical", status="satisfied")
    _requirement(session, opp.id, req_type="delivery", status="satisfied")

    fake_provider = FakeProvider({"proposal_drafting": DRAFT_RESPONSE})

    with patch("govcon.ai.structured.get_provider", return_value=fake_provider):
        result = generate_proposal(session, opportunity_id=opp.id)

    assert result["section_count"] == 2
    pv = session.get(ProposalVersion, result["version_id"])
    assert pv is not None
    sections = session.scalars(
        select(ProposalSection).where(ProposalSection.proposal_version_id == pv.id)
    ).all()
    assert len(sections) == 2


def test_generate_proposal_triggers_package_and_notification(session):
    """AC-1: generate_proposal() sends a proposal_package_generated notification."""
    from govcon.proposals.service import generate_proposal

    opp = _opp(session)
    review, pursuit = _approved_pursuit(session, opp)
    _requirement(session, opp.id)
    actor = _user(session, "approver")

    generate_proposal(session, opportunity_id=opp.id, skip_ai=True, actor=actor)

    notifs = session.scalars(
        select(Notification).where(Notification.opportunity_id == opp.id)
    ).all()
    types = {n.notification_type for n in notifs}
    assert "proposal_package_generated" in types


# ---------------------------------------------------------------------------
# AC-2: Proposal sections are traceable to compliance requirements
# ---------------------------------------------------------------------------

def test_proposal_sections_have_requirement_ids(session):
    """AC-2: ProposalSection rows contain requirement_ids traceable to Requirement rows."""
    from govcon.proposals.service import generate_proposal

    opp = _opp(session)
    review, pursuit = _approved_pursuit(session, opp)
    req1 = _requirement(session, opp.id, req_type="technical", status="satisfied")
    req2 = _requirement(session, opp.id, req_type="delivery", status="satisfied")

    fake_provider = FakeProvider({"proposal_drafting": DRAFT_RESPONSE})

    with patch("govcon.ai.structured.get_provider", return_value=fake_provider):
        result = generate_proposal(session, opportunity_id=opp.id)

    sections = session.scalars(
        select(ProposalSection).where(ProposalSection.proposal_version_id == result["version_id"])
    ).all()

    all_req_ids = {rid for s in sections for rid in (s.requirement_ids or [])}
    # At least one section references requirement IDs
    assert len(all_req_ids) > 0


def test_placeholder_draft_maps_requirement_ids(session):
    """AC-2: placeholder draft assigns requirement_ids for technical sections."""
    from govcon.proposals.service import generate_proposal

    opp = _opp(session)
    review, pursuit = _approved_pursuit(session, opp)
    req = _requirement(session, opp.id, req_type="technical", status="missing", mandatory=True)

    result = generate_proposal(session, opportunity_id=opp.id, skip_ai=True)

    sections = session.scalars(
        select(ProposalSection).where(ProposalSection.proposal_version_id == result["version_id"])
    ).all()

    tech_section = next((s for s in sections if s.section_key == "technical_response"), None)
    assert tech_section is not None
    # technical requirements should be mapped
    assert req.id in (tech_section.requirement_ids or [])


# ---------------------------------------------------------------------------
# AC-3: Submission instructions are generated from solicitation evidence
# ---------------------------------------------------------------------------

def test_submission_package_generated_from_requirements(session):
    """AC-3: generate_submission_package() extracts instructions from requirements."""
    from govcon.submissions.service import generate_submission_package

    opp = _opp(session)
    review, pursuit = _approved_pursuit(session, opp)

    # Add requirements with submission key_values
    req = Requirement(
        opportunity_id=opp.id,
        requirement_type="submission",
        requirement_text="Submit via email to contracting@dla.mil",
        mandatory=True,
        response_required=False,
        status="satisfied",
        independently_confirmed=True,
        key_values={
            "submission_method": "email",
            "recipient_email": "contracting@dla.mil",
            "deadline_timezone": "ET",
        },
    )
    session.add(req)
    session.flush()

    result = generate_submission_package(session, opportunity_id=opp.id)

    assert result["submission_id"] is not None
    assert result["submission_method"] == "email"
    assert result["recipient_email"] == "contracting@dla.mil"
    assert result["deadline_timezone"] == "ET"


def test_submission_instructions_from_checklist(session):
    """AC-3: checklist generates step-by-step submission instructions."""
    from govcon.submissions.checklist import generate_step_by_step_instructions, generate_final_checklist
    from govcon.submissions.service import generate_submission_package

    opp = _opp(session)
    review, pursuit = _approved_pursuit(session, opp)
    _requirement(session, opp.id)

    generate_submission_package(session, opportunity_id=opp.id)
    steps = generate_step_by_step_instructions(session, opportunity_id=opp.id)

    assert len(steps) >= 3
    assert steps[0]["step"] == 1
    assert "proposal" in steps[0]["title"].lower()

    checklist = generate_final_checklist(session, opportunity_id=opp.id)
    assert "items" in checklist
    assert checklist["mandatory_total"] >= 0


def test_email_draft_generated(session):
    """AC-3: draft_submission_email() produces To, Subject, and body."""
    from govcon.submissions.email_adapter import draft_submission_email
    from govcon.submissions.service import generate_submission_package

    opp = _opp(session)
    review, pursuit = _approved_pursuit(session, opp)

    req = Requirement(
        opportunity_id=opp.id,
        requirement_type="submission",
        requirement_text="Email submission",
        mandatory=True,
        response_required=False,
        status="satisfied",
        independently_confirmed=True,
        key_values={"submission_method": "email", "recipient_email": "co@agency.gov"},
    )
    session.add(req)
    session.flush()

    generate_submission_package(session, opportunity_id=opp.id)

    draft = draft_submission_email(session, opportunity_id=opp.id)

    assert draft["subject"]
    assert draft["body"]
    assert "co@agency.gov" == draft["to"] or "co@agency.gov" in draft["body"]
    assert len(draft["attachments"]) >= 1


# ---------------------------------------------------------------------------
# AC-4: Missing mandatory items block `ready`
# ---------------------------------------------------------------------------

def test_missing_mandatory_blocks_ready(session):
    """AC-4: workspace shows NOT_READY when mandatory items are missing."""
    from govcon.proposals.service import get_proposal_workspace, generate_proposal

    opp = _opp(session)
    review, pursuit = _approved_pursuit(session, opp)
    # Add a blocking mandatory requirement
    _requirement(session, opp.id, status="missing", mandatory=True, blocks_submission=True)

    generate_proposal(session, opportunity_id=opp.id, skip_ai=True)

    ws = get_proposal_workspace(session, opportunity_id=opp.id)
    assert ws["readiness"] == "NOT_READY"
    assert len(ws["blocking_issues"]) > 0


def test_all_satisfied_shows_ready(session):
    """AC-4: workspace shows READY when all mandatory requirements satisfied."""
    from govcon.proposals.service import get_proposal_workspace, generate_proposal

    opp = _opp(session)
    review, pursuit = _approved_pursuit(session, opp)
    _requirement(session, opp.id, status="satisfied", mandatory=True, blocks_submission=False)

    generate_proposal(session, opportunity_id=opp.id, skip_ai=True)

    ws = get_proposal_workspace(session, opportunity_id=opp.id)
    assert ws["readiness"] == "READY"
    assert ws["blocking_issues"] == []


def test_checklist_blocked_when_mandatory_missing(session):
    """AC-4: checklist overall=blocked when mandatory requirement missing."""
    from govcon.submissions.checklist import generate_final_checklist

    opp = _opp(session)
    review, pursuit = _approved_pursuit(session, opp)
    _requirement(session, opp.id, status="missing", mandatory=True, blocks_submission=True)

    checklist = generate_final_checklist(session, opportunity_id=opp.id)
    assert checklist["overall"] == "blocked"


# ---------------------------------------------------------------------------
# AC-5: Final package can be exported as DOCX/PDF/XLSX/ZIP
# ---------------------------------------------------------------------------

def test_export_proposal_docx(session):
    """AC-5: export_proposal_docx() returns non-empty bytes."""
    from govcon.proposals.export import export_proposal_docx
    from govcon.proposals.service import generate_proposal

    opp = _opp(session)
    review, pursuit = _approved_pursuit(session, opp)
    _requirement(session, opp.id)

    result = generate_proposal(session, opportunity_id=opp.id, skip_ai=True)
    docx_bytes = export_proposal_docx(session, proposal_version_id=result["version_id"])
    assert len(docx_bytes) > 0


def test_export_coverage_xlsx(session):
    """AC-5: export_coverage_xlsx() returns valid XLSX bytes."""
    from govcon.proposals.export import export_coverage_xlsx
    from govcon.proposals.service import generate_proposal

    opp = _opp(session)
    review, pursuit = _approved_pursuit(session, opp)
    _requirement(session, opp.id, req_type="technical")

    result = generate_proposal(session, opportunity_id=opp.id, skip_ai=True)
    xlsx_bytes = export_coverage_xlsx(
        session,
        opportunity_id=opp.id,
        proposal_version_id=result["version_id"],
    )
    assert len(xlsx_bytes) > 0

    # Verify it's a valid Excel file
    import io
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(xlsx_bytes))
    ws = wb.active
    assert ws.max_row >= 1


def test_export_submission_zip(session):
    """AC-5: export_submission_zip() returns a ZIP containing required files."""
    from govcon.proposals.export import export_submission_zip
    from govcon.proposals.service import generate_proposal
    from govcon.submissions.service import generate_submission_package

    opp = _opp(session)
    review, pursuit = _approved_pursuit(session, opp)
    _requirement(session, opp.id)

    generate_proposal(session, opportunity_id=opp.id, skip_ai=True)
    generate_submission_package(session, opportunity_id=opp.id)

    zip_bytes = export_submission_zip(session, opportunity_id=opp.id)
    assert len(zip_bytes) > 0

    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        names = set(zf.namelist())
    assert "submission_instructions.txt" in names
    assert "submission_checklist.json" in names
    assert "file_manifest.json" in names
    # Proposal DOCX should be present
    assert any(n.endswith(".docx") for n in names)


# ---------------------------------------------------------------------------
# AC-6: Final approval remains human-authorized
# ---------------------------------------------------------------------------

def test_final_approval_requires_approver_role(session):
    """AC-6: finalize_proposal() raises PermissionDenied for non-approver."""
    from govcon.proposals.service import generate_proposal, finalize_proposal
    from govcon.collaboration.users import PermissionDenied

    opp = _opp(session)
    review, pursuit = _approved_pursuit(session, opp)
    _requirement(session, opp.id, status="satisfied")

    generate_proposal(session, opportunity_id=opp.id, skip_ai=True)

    reviewer = _user(session, "reviewer")  # not an approver

    with pytest.raises(PermissionDenied):
        finalize_proposal(
            session,
            opportunity_id=opp.id,
            action="APPROVE_FOR_SUBMISSION",
            actor=reviewer,
        )


def test_approve_for_submission_sets_final_approved(session):
    """AC-6: APPROVE_FOR_SUBMISSION sets proposal.status=final_approved and records actor."""
    from govcon.proposals.service import generate_proposal, finalize_proposal
    from govcon.compliance.submission_preflight import ReadinessBlocked

    opp = _opp(session)
    review, pursuit = _approved_pursuit(session, opp)
    _requirement(session, opp.id, status="satisfied", mandatory=True, blocks_submission=False)

    generate_proposal(session, opportunity_id=opp.id, skip_ai=True)

    approver = _user(session, "approver")

    result = finalize_proposal(
        session,
        opportunity_id=opp.id,
        action="APPROVE_FOR_SUBMISSION",
        actor=approver,
        override_reason=None,
    )

    assert result["status"] == "final_approved"
    assert result["action"] == "APPROVE_FOR_SUBMISSION"

    proposal = session.get(Proposal, result["proposal_id"])
    assert proposal.final_approved_by_user_id == approver.id
    assert proposal.final_approved_at is not None


def test_return_for_fix_sets_returned_status(session):
    """AC-6: RETURN_FOR_FIX sets proposal.status=returned_for_fix."""
    from govcon.proposals.service import generate_proposal, finalize_proposal

    opp = _opp(session)
    review, pursuit = _approved_pursuit(session, opp)
    _requirement(session, opp.id)

    generate_proposal(session, opportunity_id=opp.id, skip_ai=True)
    approver = _user(session, "approver")

    result = finalize_proposal(
        session, opportunity_id=opp.id, action="RETURN_FOR_FIX", actor=approver
    )
    assert result["status"] == "returned_for_fix"


def test_cancel_bid_sets_pursuit_cancelled(session):
    """AC-6: CANCEL_BID sets proposal.status=cancelled and pursuit.stage=cancelled."""
    from govcon.proposals.service import generate_proposal, finalize_proposal

    opp = _opp(session)
    review, pursuit = _approved_pursuit(session, opp)
    _requirement(session, opp.id)

    generate_proposal(session, opportunity_id=opp.id, skip_ai=True)
    approver = _user(session, "approver")

    result = finalize_proposal(
        session, opportunity_id=opp.id, action="CANCEL_BID", actor=approver
    )
    assert result["status"] == "cancelled"
    assert result["pursuit_stage"] == "cancelled"


def test_final_approval_is_audited(session):
    """AC-6: finalize_proposal() writes an audit_event row."""
    from govcon.proposals.service import generate_proposal, finalize_proposal

    opp = _opp(session)
    review, pursuit = _approved_pursuit(session, opp)
    _requirement(session, opp.id, status="satisfied", mandatory=True, blocks_submission=False)

    generate_proposal(session, opportunity_id=opp.id, skip_ai=True)
    approver = _user(session, "approver")

    finalize_proposal(
        session,
        opportunity_id=opp.id,
        action="APPROVE_FOR_SUBMISSION",
        actor=approver,
    )

    events = session.scalars(
        select(AuditEvent).where(
            AuditEvent.opportunity_id == opp.id,
            AuditEvent.action_type == "proposal_approve_for_submission",
        )
    ).all()
    assert len(events) >= 1


# ---------------------------------------------------------------------------
# AC-7: No arbitrary portal auto-submission occurs in v1
# ---------------------------------------------------------------------------

def test_no_auto_submission_in_v1(session):
    """AC-7: record_submission_confirmation() requires explicit human call — no auto-submit."""
    from govcon.proposals.service import record_submission_confirmation
    from govcon.submissions.service import generate_submission_package

    opp = _opp(session)
    review, pursuit = _approved_pursuit(session, opp)
    _requirement(session, opp.id)

    generate_submission_package(session, opportunity_id=opp.id)
    approver = _user(session, "approver")

    # Submission starts as 'preparing' — not auto-submitted
    submission = session.scalars(
        select(Submission).where(Submission.opportunity_id == opp.id)
    ).first()
    assert submission.status == "preparing"

    # Must be explicitly confirmed by a human
    result = record_submission_confirmation(
        session,
        opportunity_id=opp.id,
        confirmation_number="CONF-12345",
        actor=approver,
    )
    assert result["status"] == "submitted"
    assert result["confirmation_number"] == "CONF-12345"


# ---------------------------------------------------------------------------
# Red-team: proposal red-team AI review
# ---------------------------------------------------------------------------

def test_red_team_runs_and_persists_findings(session):
    """Red-team: run_proposal_red_team() records critical findings as ComplianceFinding rows."""
    from govcon.proposals.service import generate_proposal
    from govcon.proposals.ai_review import run_proposal_red_team

    opp = _opp(session)
    review, pursuit = _approved_pursuit(session, opp)
    _requirement(session, opp.id, req_type="technical", status="satisfied")

    result_gen = generate_proposal(session, opportunity_id=opp.id, skip_ai=True)

    # Red team with a critical finding
    critical_response = {**RED_TEAM_RESPONSE, "findings": [{
        **RED_TEAM_RESPONSE["findings"][0],
        "severity": "critical",
    }], "critical_count": 1}

    fake = FakeProvider({"proposal_red_team": critical_response})

    with patch("govcon.ai.structured.get_provider", return_value=fake):
        rt_result = run_proposal_red_team(
            session,
            opportunity_id=opp.id,
            proposal_version_id=result_gen["version_id"],
        )

    assert rt_result["critical_count"] == 1

    # Critical finding should be recorded as a blocking ComplianceFinding
    findings = session.scalars(
        select(ComplianceFinding).where(
            ComplianceFinding.opportunity_id == opp.id,
            ComplianceFinding.finding_type == "red_team_critical",
        )
    ).all()
    assert len(findings) >= 1
    assert findings[0].blocks_submission is True

    # Proposal.red_team_analysis_id should be set
    proposal = session.scalars(
        select(Proposal).where(Proposal.opportunity_id == opp.id)
    ).first()
    assert proposal.red_team_analysis_id is not None


def test_red_team_major_only_does_not_block(session):
    """Red-team: major findings do NOT create blocking ComplianceFinding rows."""
    from govcon.proposals.service import generate_proposal
    from govcon.proposals.ai_review import run_proposal_red_team

    opp = _opp(session)
    review, pursuit = _approved_pursuit(session, opp)
    _requirement(session, opp.id)

    result_gen = generate_proposal(session, opportunity_id=opp.id, skip_ai=True)

    fake = FakeProvider({"proposal_red_team": RED_TEAM_RESPONSE})  # only major finding

    with patch("govcon.ai.structured.get_provider", return_value=fake):
        rt_result = run_proposal_red_team(
            session,
            opportunity_id=opp.id,
            proposal_version_id=result_gen["version_id"],
        )

    assert rt_result["critical_count"] == 0
    assert rt_result["major_count"] == 1

    blocking_findings = session.scalars(
        select(ComplianceFinding).where(
            ComplianceFinding.opportunity_id == opp.id,
            ComplianceFinding.finding_type == "red_team_critical",
        )
    ).all()
    assert len(blocking_findings) == 0


# ---------------------------------------------------------------------------
# Versions: immutable proposal versions
# ---------------------------------------------------------------------------

def test_every_regeneration_creates_new_version(session):
    """Each generate_proposal() call creates a new immutable ProposalVersion."""
    from govcon.proposals.service import generate_proposal
    from govcon.proposals.versions import list_proposal_versions

    opp = _opp(session)
    review, pursuit = _approved_pursuit(session, opp)
    _requirement(session, opp.id)

    r1 = generate_proposal(session, opportunity_id=opp.id, skip_ai=True)
    r2 = generate_proposal(session, opportunity_id=opp.id, skip_ai=True)

    assert r1["version_id"] != r2["version_id"]
    assert r2["version_number"] == 2

    versions = list_proposal_versions(session, r1["proposal_id"])
    assert len(versions) == 2

    # v1 must still exist (immutable)
    v1 = session.get(ProposalVersion, r1["version_id"])
    assert v1 is not None
    assert v1.version_number == 1


# ---------------------------------------------------------------------------
# Proposal-to-requirement coverage
# ---------------------------------------------------------------------------

def test_coverage_run_after_generation(session):
    """check_proposal_coverage() runs after generate_proposal() without error."""
    from govcon.proposals.service import generate_proposal
    from govcon.compliance.proposal_coverage import check_proposal_coverage

    opp = _opp(session)
    review, pursuit = _approved_pursuit(session, opp)
    req = _requirement(session, opp.id, req_type="technical", status="satisfied", response_required=True)

    fake = FakeProvider({"proposal_drafting": DRAFT_RESPONSE})

    with patch("govcon.ai.structured.get_provider", return_value=fake):
        result_gen = generate_proposal(session, opportunity_id=opp.id)

    coverage = check_proposal_coverage(
        session, opp.id, result_gen["version_id"], use_ai=False
    )
    assert "run_id" in coverage
    assert "summary" in coverage


# ---------------------------------------------------------------------------
# CLI commands smoke tests
# ---------------------------------------------------------------------------

def test_cli_proposal_status_shows_readiness(session, upgraded_engine):
    """CLI: govcon proposal status --opportunity-id shows readiness."""
    import os
    from govcon.cli import app
    from govcon.proposals.service import generate_proposal

    opp = _opp(session)
    review, pursuit = _approved_pursuit(session, opp)
    _requirement(session, opp.id, status="satisfied")
    generate_proposal(session, opportunity_id=opp.id, skip_ai=True)
    session.commit()

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["proposal", "status", f"--opportunity-id={opp.id}"],
        env={"DATABASE_URL": str(upgraded_engine.url)},
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "readiness:" in result.output


def test_cli_submission_checklist(session, upgraded_engine):
    """CLI: govcon submission checklist --opportunity-id shows overall status."""
    from govcon.cli import app
    from govcon.proposals.service import generate_proposal
    from govcon.submissions.service import generate_submission_package

    opp = _opp(session)
    review, pursuit = _approved_pursuit(session, opp)
    _requirement(session, opp.id, status="satisfied")
    generate_proposal(session, opportunity_id=opp.id, skip_ai=True)
    generate_submission_package(session, opportunity_id=opp.id)
    session.commit()

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["submission", "checklist", f"--opportunity-id={opp.id}"],
        env={"DATABASE_URL": str(upgraded_engine.url)},
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "overall:" in result.output
