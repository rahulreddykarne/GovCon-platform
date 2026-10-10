"""Workflow gate hardening (C1–C5).

- Transition tables reject every illegal state change.
- PROPRIETARY data is refused by the AI policy under the default settings.
- MCP write tools act only as the pinned actor and cannot set gated stages.
- Proposal approval and submission confirmation enforce their preconditions.
- A material source change after final approval reopens review and
  invalidates the proposal approval, submission readiness, and pursuit stage.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.ai.structured import StructuredCallError, run_structured_prompt
from govcon.collaboration.assignments import assign_reviewer
from govcon.collaboration.comments import add_comment
from govcon.collaboration.review_sessions import (
    ReviewWorkflowError,
    complete_assignment,
    ensure_review_session,
    finalize_approval,
    recalculate_quorum,
)
from govcon.collaboration.users import PermissionDenied, invite_user
from govcon.compliance.submission_preflight import ReadinessBlocked
from govcon.models import (
    AIAnalysis,
    ComplianceFinding,
    Notification,
    Opportunity,
    OpportunityEvent,
    Proposal,
    Pursuit,
    ReviewAssignment,
    ReviewSession,
    Submission,
    User,
)
from govcon.proposals.service import (
    finalize_proposal,
    generate_proposal,
    record_submission_confirmation,
)
from govcon.proposals.versions import ProposalWorkflowError, create_proposal_version
from govcon.security.classification import DataClassification
from govcon.workflow.transitions import (
    APPROVABLE_PROPOSAL_STATUSES,
    InvalidTransition,
    can_transition,
    require_transition,
)

OVERRIDE = "Approver verified the package manually; pre-flight not run in this test."


@pytest.fixture(autouse=True)
def bootstrap_prompt_files(monkeypatch):
    # Pure policy tests deliberately have no registry session.
    monkeypatch.setenv("PROMPT_ALLOW_DISK_FALLBACK", "true")


@pytest.fixture()
def session(upgraded_engine):
    with Session(upgraded_engine) as s:
        yield s
        s.rollback()


def _uid() -> str:
    return uuid4().hex[:10]


def _user(session: Session, role: str) -> User:
    return invite_user(
        session,
        email=f"{role}-{_uid()}@gates.test",
        display_name=f"{role} gate",
        password="correct horse battery",
        role=role,
    )


def _opp(session: Session, **fields) -> Opportunity:
    row = Opportunity(
        source="sam",
        source_id=f"gate-{_uid()}",
        title="Gate fixture",
        status="open",
        psc_code="6515",
        response_deadline=datetime.now(UTC) + timedelta(days=20),
        raw={"fixture": True},
        links={},
        **fields,
    )
    session.add(row)
    session.flush()
    return row


def _review_version(session: Session, opp_id: int) -> int:
    return ensure_review_session(session, opportunity_id=opp_id).version


def _approve_bid(session: Session, opp_id: int) -> tuple[User, User]:
    """Real quorum: an assigned reviewer completes, an approver approves."""
    approver = _user(session, "approver")
    reviewer = _user(session, "reviewer")
    assign_reviewer(session, opportunity_id=opp_id, user_id=reviewer.id)
    add_comment(
        session,
        opportunity_id=opp_id,
        user_id=reviewer.id,
        body="Scope, delivery, and pricing reviewed; proceed with the bid.",
        validate_with_ai=False,
    )
    complete_assignment(
        session, opportunity_id=opp_id, user_id=reviewer.id, action="approve_continue", recommendation="bid"
    )
    finalize_approval(
        session,
        opportunity_id=opp_id,
        actor=approver,
        action="approve_to_bid",
        expected_version=_review_version(session, opp_id),
    )
    return approver, reviewer


def _final_approve(session: Session, opp_id: int, approver: User) -> Proposal:
    generate_proposal(session, opportunity_id=opp_id, actor=approver, skip_ai=True)
    proposal = session.scalar(select(Proposal).where(Proposal.opportunity_id == opp_id))
    from hashlib import sha256

    from govcon.compliance.deterministic import PackageFile, SubmissionPackage
    from govcon.compliance.matrix import record_run
    from govcon.proposals.export import export_proposal_docx
    from govcon.submissions.manifest import snapshot_package
    from govcon.submissions.service import generate_submission_package
    generate_submission_package(session, opportunity_id=opp_id, actor=approver)
    content = export_proposal_docx(session, proposal_version_id=proposal.current_version_id)
    import tempfile
    from pathlib import Path
    artifact = Path(tempfile.mkdtemp()) / "proposal.docx"
    artifact.write_bytes(content)
    package = SubmissionPackage(files=[PackageFile("proposal.docx", role="proposal", size_bytes=len(content), sha256=sha256(content).hexdigest(), local_path=str(artifact))], proposal_version_id=proposal.current_version_id)
    snapshot_package(session, _submission(session, opp_id), package, actor=approver)
    record_run(session, opportunity_id=opp_id, run_type="submission_preflight", run_version="test", output={"package": package.manifest(), "ready": False})
    finalize_proposal(
        session,
        opportunity_id=opp_id,
        action="APPROVE_FOR_SUBMISSION",
        actor=approver,
        expected_version=proposal.version,
        override_reason=OVERRIDE,
    )
    session.refresh(proposal)
    return proposal


def _submission(session: Session, opp_id: int) -> Submission:
    row = session.scalar(
        select(Submission).where(Submission.opportunity_id == opp_id).order_by(Submission.id.desc())
    )
    assert row is not None
    session.refresh(row)
    return row


def _pursuit(session: Session, opp_id: int) -> Pursuit:
    row = session.scalar(select(Pursuit).where(Pursuit.opportunity_id == opp_id))
    assert row is not None
    session.refresh(row)
    return row


def _review(session: Session, opp_id: int) -> ReviewSession:
    row = session.scalar(select(ReviewSession).where(ReviewSession.opportunity_id == opp_id))
    assert row is not None
    session.refresh(row)
    return row


# ── transition tables (C4) ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kind", "current", "target", "allowed"),
    [
        ("pursuit", "evaluating", "bid_approved", True),
        ("pursuit", "evaluating", "ready_to_submit", False),
        ("pursuit", "evaluating", "submitted", False),
        ("pursuit", "evaluating", "won", False),
        ("pursuit", "bid_approved", "ready_to_submit", True),
        ("pursuit", "review", "ready_to_submit", True),
        ("pursuit", "ready_to_submit", "submitted", True),
        ("pursuit", "ready_to_submit", "won", False),
        ("pursuit", "submitted", "won", True),
        ("pursuit", "submitted", "evaluating", False),
        ("pursuit", "won", "lost", False),
        ("pursuit", "cancelled", "evaluating", False),
        ("pursuit", "no_bid", "evaluating", True),
        ("proposal", "draft", "final_approved", False),
        ("proposal", "ai_generated", "final_approved", True),
        ("proposal", "red_teamed", "final_approved", True),
        ("proposal", "returned_for_fix", "final_approved", False),
        ("proposal", "cancelled", "draft", False),
        ("proposal", "final_approved", "draft", True),
        ("submission", "preparing", "submitted", False),
        ("submission", "ready", "submitted", True),
        ("submission", "submitted", "ready", False),
        ("submission", "withdrawn", "ready", False),
        ("review_session", "approved_to_bid", "no_bid", False),
        ("review_session", "approved_to_bid", "under_review", True),
        ("review_session", "approval_pending", "approved_to_bid", True),
        ("review_session", "no_bid", "approved_to_bid", False),
    ],
)
def test_transition_table(kind: str, current: str, target: str, allowed: bool) -> None:
    assert can_transition(kind, current, target) is allowed
    if allowed:
        require_transition(kind, current, target)
    else:
        with pytest.raises(InvalidTransition):
            require_transition(kind, current, target)


def test_unknown_target_state_is_rejected() -> None:
    assert not can_transition("pursuit", "evaluating", "teleported")
    assert APPROVABLE_PROPOSAL_STATUSES == {"ai_generated", "red_teamed"}


# ── AI data-classification policy (C2) ────────────────────────────────────────


class _MustNotBeCalled:
    name = "deepseek"

    def complete(self, **_kwargs):  # pragma: no cover - reaching here is the failure
        raise AssertionError("the provider must not be called when policy refuses")


PROPRIETARY_PROMPTS = [
    "proposal_drafting",
    "proposal_red_team",
    "reviewer_comment_validation",
    "consolidated_review",
    "compliance_validator",
    "compliance_red_team",
    "proposal_coverage",
    "submission_preflight_ai",
    "outcome_analysis",
    "market_analysis",
    "supplier_analysis",
    "pricing_analysis",
]


@pytest.mark.parametrize("prompt_name", PROPRIETARY_PROMPTS)
def test_proprietary_calls_are_blocked_under_default_policy(prompt_name: str) -> None:
    with patch("govcon.ai.structured.get_provider", return_value=_MustNotBeCalled()), pytest.raises(StructuredCallError) as exc:
        run_structured_prompt(
            None,
            opportunity_id=None,
            prompt_name=prompt_name,
            analysis_type="gate_test",
            variables={},
            context_manifest={},
            classification=DataClassification.PROPRIETARY,
        )
    assert exc.value.reason == "blocked_by_policy"


@pytest.mark.parametrize("prompt_name", ["requirement_extraction_a", "solicitation_analysis", "amendment_analysis"])
def test_public_only_prompt_refuses_proprietary_even_when_gateway_allows(prompt_name, monkeypatch) -> None:
    from govcon.config import get_settings

    monkeypatch.setenv("AI_EXTERNAL_ALLOWED_FOR_PROPRIETARY", "true")
    get_settings.cache_clear()
    with patch("govcon.ai.structured.get_provider", return_value=_MustNotBeCalled()), pytest.raises(StructuredCallError) as exc:
        run_structured_prompt(
            None,
            opportunity_id=None,
            prompt_name=prompt_name,
            analysis_type="gate_test",
            variables={},
            context_manifest={},
            classification=DataClassification.PROPRIETARY,
        )
    assert exc.value.reason == "blocked_by_policy"
    assert "does not allow PROPRIETARY" in exc.value.detail


def test_classification_is_required() -> None:
    with pytest.raises(TypeError):
        run_structured_prompt(  # type: ignore[call-arg]
            None,
            opportunity_id=None,
            prompt_name="requirement_extraction_a",
            analysis_type="gate_test",
            variables={},
            context_manifest={},
        )


def test_proposal_drafting_is_sent_as_proprietary_and_blocked(session) -> None:
    from govcon.proposals.drafting import draft_proposal

    opp = _opp(session)
    session.add(Pursuit(opportunity_id=opp.id, stage="bid_approved", quote_price=100, sourcing_cost=80, supplier="Acme"))
    session.flush()
    with patch("govcon.ai.structured.get_provider", return_value=_MustNotBeCalled()), pytest.raises(StructuredCallError) as exc:
        draft_proposal(session, opportunity_id=opp.id, company_facts={"name": "Us"})
    assert exc.value.reason == "blocked_by_policy"


def test_reviewer_comment_validation_is_blocked_by_default(session) -> None:
    from govcon.collaboration.ai_comment_review import (
        AICommentValidationError,
        validate_comment_with_ai,
    )

    opp = _opp(session)
    reviewer = _user(session, "reviewer")
    assign_reviewer(session, opportunity_id=opp.id, user_id=reviewer.id)
    comment = add_comment(
        session,
        opportunity_id=opp.id,
        user_id=reviewer.id,
        body="Supplier quote at 82 dollars is well under the historical price.",
        validate_with_ai=False,
    )
    with patch("govcon.ai.structured.get_provider", return_value=_MustNotBeCalled()), pytest.raises(AICommentValidationError) as exc:
        validate_comment_with_ai(session, comment=comment)
    assert exc.value.reason == "blocked_by_policy"


def test_jev_blocked_by_policy_falls_back_to_rules(session, monkeypatch) -> None:
    from govcon.config import get_settings
    from govcon.decision.engine import run_decision_bundle

    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setenv("JEV_API_KEY", "test-key-not-used-1234")
    monkeypatch.setenv("DECISION_PRIMARY_PROVIDER", "jev")
    get_settings.cache_clear()
    opp = _opp(session)
    import logging

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    engine_logger = logging.getLogger("govcon.decision.engine")
    handler = _Capture(level=logging.WARNING)
    engine_logger.addHandler(handler)
    try:
        with patch("httpx.Client.post", side_effect=AssertionError("JEV must not be called")):
            execution = run_decision_bundle(session, opportunity_id=opp.id, bundle_name="bid_decision")
    finally:
        engine_logger.removeHandler(handler)
    assert execution.provider == "rules"
    assert any("JEV unavailable" in r.getMessage() for r in records)


def test_deepseek_errors_do_not_log_response_bodies(monkeypatch, caplog) -> None:
    import httpx

    from govcon.ai.providers.deepseek import DeepSeekAPIError, DeepSeekProvider
    from govcon.config import Settings

    secret_echo = "PROMPT-ECHO-quote_price=12345"

    class _Resp:
        status_code = 500
        text = secret_echo

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    provider = DeepSeekProvider(api_key="k" * 20, settings=Settings())
    with caplog.at_level("ERROR"), pytest.raises(DeepSeekAPIError):
        provider.complete(
            system_prompt="s",
            user_prompt="u",
            classification=DataClassification.PUBLIC,
            purpose="gate_test",
        )
    assert secret_echo not in caplog.text


# ── MCP actor and gated stages (C3) ───────────────────────────────────────────


def test_mcp_write_tools_require_the_configured_actor(session, monkeypatch) -> None:
    from govcon.mcp import operations as ops
    from govcon.mcp.context import reset_server_actor

    reset_server_actor()
    monkeypatch.delenv("MCP_ACTOR_EMAIL", raising=False)
    opp = _opp(session)
    with pytest.raises(PermissionDenied, match="MCP_ACTOR_EMAIL"):
        ops.op_add_pursuit(session, opp.id)


def test_mcp_update_pursuit_cannot_set_gated_stages_and_checks_version(session, monkeypatch) -> None:
    from govcon.mcp import operations as ops
    from govcon.mcp.context import reset_server_actor

    owner = _user(session, "owner")
    reset_server_actor()
    monkeypatch.setenv("MCP_ACTOR_EMAIL", owner.email)
    opp = _opp(session)
    ops.op_add_pursuit(session, opp.id)
    pursuit = _pursuit(session, opp.id)
    for gated in ("bid_approved", "ready_to_submit", "submitted", "won", "cancelled"):
        with pytest.raises(ValueError):
            ops.op_update_pursuit(session, opp.id, expected_version=pursuit.version, stage=gated)
    with pytest.raises(ValueError, match="stale"):
        ops.op_update_pursuit(session, opp.id, expected_version=pursuit.version + 5, quote_price=10.0)
    ok = ops.op_update_pursuit(session, opp.id, expected_version=pursuit.version, stage="sourcing", quote_price=10.0)
    assert ok["data"]["stage"] == "sourcing"
    reset_server_actor()


def test_mcp_reviewer_actor_cannot_assign_reviewers(session, monkeypatch) -> None:
    from govcon.mcp import operations as ops
    from govcon.mcp.context import reset_server_actor

    reviewer = _user(session, "reviewer")
    other = _user(session, "reviewer")
    reset_server_actor()
    monkeypatch.setenv("MCP_ACTOR_EMAIL", reviewer.email)
    opp = _opp(session)
    with pytest.raises(PermissionDenied):
        ops.op_assign_reviewer(session, opp.id, user_email=other.email)
    reset_server_actor()


# ── review workflow (C1 / quorum regressions) ────────────────────────────────


def test_complete_review_requires_review_permission_and_counts_once(session) -> None:
    opp = _opp(session)
    viewer = _user(session, "read_only")
    reviewer = _user(session, "reviewer")
    ensure_review_session(session, opportunity_id=opp.id)
    with pytest.raises((PermissionDenied, ReviewWorkflowError)):
        complete_assignment(
            session, opportunity_id=opp.id, user_id=viewer.id, action="approve_continue", agree_with_ai_assessment=True
        )
    assign_reviewer(session, opportunity_id=opp.id, user_id=reviewer.id)
    for _ in range(3):
        complete_assignment(
            session, opportunity_id=opp.id, user_id=reviewer.id, action="approve_continue", agree_with_ai_assessment=True
        )
    assert _review(session, opp.id).completed_review_count == 1


def test_approval_survives_recalculation_and_does_not_repeat_notifications(session) -> None:
    from govcon.models import DecisionRun

    def synthesis_runs() -> int:
        return len(
            session.scalars(
                select(DecisionRun).where(
                    DecisionRun.opportunity_id == opp.id,
                    DecisionRun.bundle_name == "collaborative_review_synthesis",
                )
            ).all()
        )

    opp = _opp(session)
    _approve_bid(session, opp.id)
    before = len(session.scalars(select(Notification).where(Notification.opportunity_id == opp.id)).all())
    runs_before = synthesis_runs()
    assert runs_before == 1  # consolidated once, when quorum was first met
    for _ in range(3):
        recalculate_quorum(session, opportunity_id=opp.id)
    review = _review(session, opp.id)
    assert review.status == "approved_to_bid"
    after = len(session.scalars(select(Notification).where(Notification.opportunity_id == opp.id)).all())
    assert after == before
    assert synthesis_runs() == runs_before


def test_repeated_or_stale_approval_is_refused(session) -> None:
    opp = _opp(session)
    approver, _ = _approve_bid(session, opp.id)
    with pytest.raises(ReviewWorkflowError):
        finalize_approval(
            session,
            opportunity_id=opp.id,
            actor=approver,
            action="approve_to_bid",
            expected_version=_review_version(session, opp.id),
        )
    with pytest.raises(ReviewWorkflowError, match="stale"):
        finalize_approval(
            session,
            opportunity_id=opp.id,
            actor=approver,
            action="return_for_review",
            expected_version=_review_version(session, opp.id) - 1,
        )


def test_return_for_review_reopens_reviews_and_revokes_downstream_approval(session) -> None:
    opp = _opp(session)
    approver, _ = _approve_bid(session, opp.id)
    _final_approve(session, opp.id, approver)
    assert _pursuit(session, opp.id).stage == "ready_to_submit"

    finalize_approval(
        session,
        opportunity_id=opp.id,
        actor=approver,
        action="return_for_review",
        expected_version=_review_version(session, opp.id),
    )
    assert _review(session, opp.id).status == "returned_for_review"
    assignments = session.scalars(select(ReviewAssignment).where(ReviewAssignment.opportunity_id == opp.id)).all()
    assert assignments and all(a.status == "reopened" for a in assignments)
    proposal = session.scalar(select(Proposal).where(Proposal.opportunity_id == opp.id))
    session.refresh(proposal)
    assert proposal.status == "returned_for_fix" and proposal.approved_version_id is None
    sub = _submission(session, opp.id)
    assert sub.status == "preparing" and sub.readiness_status == "not_ready"
    pursuit = _pursuit(session, opp.id)
    assert pursuit.stage == "evaluating" and pursuit.approved_to_bid_at is None


# ── proposal approval and submission confirmation (C4) ────────────────────────


def test_draft_proposal_cannot_be_approved(session) -> None:
    opp = _opp(session)
    approver, _ = _approve_bid(session, opp.id)
    generate_proposal(session, opportunity_id=opp.id, actor=approver, skip_ai=True)
    proposal = session.scalar(select(Proposal).where(Proposal.opportunity_id == opp.id))
    create_proposal_version(
        session, proposal_id=proposal.id, created_by=approver.email, sections=[{"section_key": "s", "content": "edit"}]
    )
    session.refresh(proposal)
    assert proposal.status == "draft"
    with pytest.raises(ProposalWorkflowError, match="cannot be approved"):
        finalize_proposal(
            session,
            opportunity_id=opp.id,
            action="APPROVE_FOR_SUBMISSION",
            actor=approver,
            expected_version=proposal.version,
            override_reason=OVERRIDE,
        )


def test_blocked_gate_changes_nothing(session) -> None:
    opp = _opp(session)
    approver, _ = _approve_bid(session, opp.id)
    generate_proposal(session, opportunity_id=opp.id, actor=approver, skip_ai=True)
    proposal = session.scalar(select(Proposal).where(Proposal.opportunity_id == opp.id))
    with pytest.raises(ReadinessBlocked):
        finalize_proposal(
            session,
            opportunity_id=opp.id,
            action="APPROVE_FOR_SUBMISSION",
            actor=approver,
            expected_version=proposal.version,
        )
    session.refresh(proposal)
    assert proposal.status == "ai_generated" and proposal.approved_version_id is None
    assert _pursuit(session, opp.id).stage == "bid_approved"


def test_new_version_after_approval_resets_approval_and_readiness(session) -> None:
    opp = _opp(session)
    approver, _ = _approve_bid(session, opp.id)
    proposal = _final_approve(session, opp.id, approver)
    approved_version = proposal.approved_version_id
    assert approved_version == proposal.current_version_id
    assert _submission(session, opp.id).status == "ready"

    create_proposal_version(
        session,
        proposal_id=proposal.id,
        created_by=approver.email,
        sections=[{"section_key": "cover_letter", "content": "Edited after approval"}],
    )
    session.refresh(proposal)
    assert proposal.status == "draft"
    assert proposal.approved_version_id is None
    assert proposal.current_version_id != approved_version
    sub = _submission(session, opp.id)
    assert sub.status == "preparing" and sub.readiness_status == "not_ready"
    assert _pursuit(session, opp.id).stage == "drafting"


def test_submission_confirmation_preconditions(session) -> None:
    opp = _opp(session)
    approver, _ = _approve_bid(session, opp.id)
    _final_approve(session, opp.id, approver)
    sub = _submission(session, opp.id)
    with pytest.raises(ProposalWorkflowError, match="future"):
        record_submission_confirmation(
            session,
            opportunity_id=opp.id,
            actor=approver,
            expected_version=sub.version,
            confirmation_number="X-1",
            submitted_at=datetime.now(UTC) + timedelta(days=1),
        )
    past_deadline = datetime.now(UTC) - timedelta(hours=1)
    opp.response_deadline = past_deadline
    sub.submission_deadline = past_deadline
    session.flush()
    with pytest.raises(ProposalWorkflowError, match="after the response deadline"):
        record_submission_confirmation(
            session, opportunity_id=opp.id, actor=approver, expected_version=sub.version, confirmation_number="X-1"
        )
    ok = record_submission_confirmation(
        session,
        opportunity_id=opp.id,
        actor=approver,
        expected_version=sub.version,
        confirmation_number="X-1",
        submitted_at=datetime.now(UTC) - timedelta(hours=2),
    )
    assert ok["status"] == "submitted"
    assert _pursuit(session, opp.id).stage == "submitted"


def test_outcome_won_requires_a_recorded_submission(session) -> None:
    from govcon.learning.outcomes import record_outcome

    opp = _opp(session)
    approver, _ = _approve_bid(session, opp.id)
    with pytest.raises(ValueError, match="cannot move"):
        record_outcome(session, opportunity_id=opp.id, outcome="won", actor=approver)
    viewer = _user(session, "reviewer")
    with pytest.raises(PermissionDenied):
        record_outcome(session, opportunity_id=opp.id, outcome="no_bid", actor=viewer)


def test_recording_the_same_outcome_twice_keeps_one_row(session) -> None:
    from govcon.learning.outcomes import record_outcome
    from govcon.models import OutcomeFeedback

    opp = _opp(session)
    approver = _user(session, "approver")
    record_outcome(session, opportunity_id=opp.id, outcome="no_bid", actor=approver, no_bid_category="margin")
    record_outcome(session, opportunity_id=opp.id, outcome="no_bid", actor=approver, no_bid_category="deadline")
    rows = session.scalars(select(OutcomeFeedback).where(OutcomeFeedback.opportunity_id == opp.id)).all()
    assert len(rows) == 1 and rows[0].no_bid_category == "deadline"


# ── material source change after approval (C5) ────────────────────────────────


def _sam_raw(notice_id: str, *, links: list[str]) -> dict:
    return {
        "noticeId": notice_id,
        "title": "Gate amendment fixture",
        "solicitationNumber": f"SOL-{notice_id[:6]}",
        "postedDate": "2026-09-20",
        "type": "Solicitation",
        "active": "Yes",
        "classificationCode": "6515",
        "naicsCode": "339113",
        "responseDeadLine": (datetime.now(UTC) + timedelta(days=20)).isoformat(),
        "resourceLinks": links,
    }


def test_amendment_after_final_approval_reopens_and_invalidates_everything(session) -> None:
    from govcon.ingest.sam_opportunities import normalize_opportunity
    from govcon.ingest.snapshots import upsert_opportunity
    from govcon.workflow.invalidation import process_pending_source_changes

    notice = f"gate-{_uid()}"
    assert upsert_opportunity(session, normalize_opportunity(_sam_raw(notice, links=[]))) == "inserted"
    opp = session.scalar(select(Opportunity).where(Opportunity.source_id == notice))
    approver, _ = _approve_bid(session, opp.id)
    _final_approve(session, opp.id, approver)
    assert _pursuit(session, opp.id).stage == "ready_to_submit"
    assert _submission(session, opp.id).status == "ready"

    # SAM publishes an amendment: a new attachment appears.
    changed = _sam_raw(notice, links=["https://sam.example.test/amendment-0001.pdf"])
    changed["responseDeadLine"] = opp.raw["responseDeadLine"]
    assert upsert_opportunity(session, normalize_opportunity(changed)) == "updated"
    marker = session.scalar(
        select(OpportunityEvent).where(
            OpportunityEvent.opportunity_id == opp.id,
            OpportunityEvent.event_type == "material_source_change",
        )
    )
    assert marker is not None and marker.new_value["value"]["level"] == "material"

    summary = process_pending_source_changes(
        session, fetch_attachments=False, use_ai=False, opportunity_ids={opp.id}
    )
    assert summary["errors"] == []

    review = _review(session, opp.id)
    assert review.status in {"under_review", "ready_for_review"}
    assert review.final_approval_status is None
    assignments = session.scalars(select(ReviewAssignment).where(ReviewAssignment.opportunity_id == opp.id)).all()
    assert all(a.status != "complete" for a in assignments)
    proposal = session.scalar(select(Proposal).where(Proposal.opportunity_id == opp.id))
    session.refresh(proposal)
    assert proposal.status == "returned_for_fix" and proposal.approved_version_id is None
    sub = _submission(session, opp.id)
    assert sub.status == "preparing" and sub.readiness_status == "not_ready"
    assert _pursuit(session, opp.id).stage == "evaluating"

    # Handled once: a second run finds nothing to do.
    again = process_pending_source_changes(session, fetch_attachments=False, use_ai=False, opportunity_ids={opp.id})
    assert again["events"] == 0


def test_override_only_approval_reopens_on_amendment_finding(session) -> None:
    from govcon.collaboration.review_sessions import apply_material_amendment_reopen

    opp = _opp(session)
    approver = _user(session, "approver")
    finalize_approval(
        session,
        opportunity_id=opp.id,
        actor=approver,
        action="approve_to_bid",
        expected_version=_review_version(session, opp.id),
        override_reason="Urgent: no reviewer available before the deadline.",
    )
    assert _review(session, opp.id).status == "approved_to_bid"
    session.add(
        ComplianceFinding(
            opportunity_id=opp.id,
            finding_type="review_reopen_required",
            severity="high",
            description="Amendment 0002 changed delivery terms after approval.",
            detected_by="amendment_revalidation",
            blocks_submission=True,
        )
    )
    session.flush()
    assert apply_material_amendment_reopen(session, opportunity_id=opp.id, actor=approver) is True
    review = _review(session, opp.id)
    assert review.status == "ready_for_review"
    assert review.final_approval_status is None and review.override_used is False


def test_stale_decision_package_blocks_approval(session) -> None:
    from govcon.workflow.source_revision import SOURCE_REVISION_KEY

    opp = _opp(session)
    reviewer = _user(session, "reviewer")
    approver = _user(session, "approver")
    package = AIAnalysis(
        opportunity_id=opp.id,
        analysis_type="decision_package",
        output_json={"summary": "old"},
        context_manifest={SOURCE_REVISION_KEY: "superseded-revision"},
    )
    session.add(package)
    session.flush()
    review = ensure_review_session(session, opportunity_id=opp.id)
    review.ai_decision_package_id = package.id
    assign_reviewer(session, opportunity_id=opp.id, user_id=reviewer.id)
    complete_assignment(
        session, opportunity_id=opp.id, user_id=reviewer.id, action="approve_continue", agree_with_ai_assessment=True
    )
    with pytest.raises(ReviewWorkflowError, match="superseded source"):
        finalize_approval(
            session,
            opportunity_id=opp.id,
            actor=approver,
            action="approve_to_bid",
            expected_version=_review_version(session, opp.id),
        )


def test_changed_recommendation_after_approval_requires_reopen(session) -> None:
    from govcon.decision.engine import run_preliminary_decision_package
    from govcon.models import BidDecision

    opp = _opp(session)
    _approve_bid(session, opp.id)
    session.add(
        BidDecision(
            opportunity_id=opp.id,
            recommendation="bid",
            strengths={"items": []},
            risks={"items": []},
            missing_information={"items": []},
            evidence={"items": []},
            rules_result={},
        )
    )
    session.flush()
    run_preliminary_decision_package(session, opportunity_id=opp.id)
    new_bid = session.scalar(
        select(BidDecision).where(BidDecision.opportunity_id == opp.id).order_by(BidDecision.id.desc())
    )
    finding = session.scalar(
        select(ComplianceFinding).where(
            ComplianceFinding.opportunity_id == opp.id,
            ComplianceFinding.finding_type == "review_reopen_required",
            ComplianceFinding.status == "open",
        )
    )
    assert (finding is not None) == (new_bid.recommendation != "bid")


def test_stale_solicitation_analysis_is_not_reused(session) -> None:
    from govcon.decision.engine import build_decision_state
    from govcon.workflow.source_revision import SOURCE_REVISION_KEY

    opp = _opp(session)
    session.add(
        AIAnalysis(
            opportunity_id=opp.id,
            analysis_type="solicitation_summary",
            schema_version="solicitation_analysis.v1",
            output_json={"items": [{"name": "widget"}], "summary": "old source"},
            context_manifest={SOURCE_REVISION_KEY: "superseded-revision"},
        )
    )
    session.flush()
    state = build_decision_state(session, opp.id)
    assert state["sourcing"]["product_found"] is None
    assert state["analysis"]["summary"] is None
    assert any("stale" in item for item in state["analysis"]["missing_information"])


def test_consolidated_review_runs_once_per_distinct_completed_set(session) -> None:
    """H10: recalculating an approval-pending session does not repeat AI/JEV work."""
    from govcon.models import DecisionRun

    opp = _opp(session)
    reviewer = _user(session, "reviewer")
    assign_reviewer(session, opportunity_id=opp.id, user_id=reviewer.id)
    complete_assignment(
        session, opportunity_id=opp.id, user_id=reviewer.id, action="approve_continue", agree_with_ai_assessment=True
    )
    assert _review(session, opp.id).status == "approval_pending"
    count = lambda: len(session.scalars(select(DecisionRun).where(DecisionRun.opportunity_id == opp.id, DecisionRun.bundle_name == "collaborative_review_synthesis")).all())
    first = count()
    for _ in range(3):
        recalculate_quorum(session, opportunity_id=opp.id)
    assert count() == first
    assert _review(session, opp.id).status == "approval_pending"

    # A new completed review changes the set, so synthesis runs again.
    second = _user(session, "reviewer")
    assign_reviewer(session, opportunity_id=opp.id, user_id=second.id)
    complete_assignment(
        session, opportunity_id=opp.id, user_id=second.id, action="approve_continue", agree_with_ai_assessment=True
    )
    assert count() == first + 1
