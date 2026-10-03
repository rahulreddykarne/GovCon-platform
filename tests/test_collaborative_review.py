"""Phase 10 tests: collaborative review, AI validation, quorum, and approval."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.ai.providers.deepseek import DeepSeekResult
from govcon.collaboration.assignments import assign_reviewer
from govcon.collaboration.comments import add_comment
from govcon.collaboration.review_sessions import (
    ReviewWorkflowError,
    apply_material_amendment_reopen,
    complete_assignment,
    ensure_review_session,
    finalize_approval,
    review_workspace,
    set_review_policy,
)
from govcon.collaboration.users import PermissionDenied, invite_user
from govcon.models import (
    AIAnalysis,
    AuditEvent,
    ComplianceFinding,
    Opportunity,
    ReviewAssignment,
    ReviewComment,
    ReviewSession,
    User,
)


class _FakeProvider:
    name = "deepseek"

    def complete(self, *, model=None, **_kwargs):
        payload = {
            "position": "disagree",
            "confidence": "medium",
            "reason": "Delivery assumption conflicts with cited source evidence.",
            "supporting_evidence": [
                {
                    "source_file_id": 1,
                    "page": 2,
                    "quote": "Delivery shall be 30 days ARO.",
                }
            ],
            "contradicting_evidence": [
                {
                    "source_file_id": 2,
                    "page": 1,
                    "quote": "Lead time currently 45 days.",
                }
            ],
            "missing_information": ["Supplier expedited lead-time confirmation"],
            "suggested_action": "Request second review on delivery risk.",
        }
        return DeepSeekResult(
            content=json.dumps(payload),
            model=model or "deepseek-flash",
            usage={"total_tokens": 25},
            latency_ms=5,
        )


# Mocked AI here sends PROPRIETARY data; the default policy blocks that.
pytestmark = pytest.mark.usefixtures("allow_proprietary_ai")

@pytest.fixture()
def session(upgraded_engine):
    with Session(upgraded_engine) as s:
        yield s
        s.rollback()


def _opp(session: Session) -> Opportunity:
    row = Opportunity(
        source="sam",
        source_id=f"phase10-{uuid4().hex}",
        title="Phase 10 collaborative review fixture",
        status="open",
        psc_code="6515",
        naics_code="339113",
        set_aside_code="SBA",
        response_deadline=datetime.now(UTC) + timedelta(days=12),
        raw={"fixture": True},
        links={},
    )
    session.add(row)
    session.flush()
    return row


def _user(session: Session, role: str, tag: str) -> User:
    return invite_user(
        session,
        email=f"{role}-{tag}-{uuid4().hex[:8]}@example.test",
        display_name=f"{role}-{tag}",
        password="correct horse battery",
        role=role,
    )


def _attach_decision_package(session: Session, opp: Opportunity) -> ReviewSession:
    analysis = AIAnalysis(
        opportunity_id=opp.id,
        analysis_type="decision_package",
        provider="fixture",
        model="fixture-v1",
        prompt_name="jev_decision_package",
        prompt_version="v1",
        prompt_hash="f" * 64,
        schema_version="decision_package.v1",
        generation_settings={},
        input_snapshot_hash="a" * 64,
        context_manifest={"opportunity_id": opp.id},
        output_json={"summary": "Shared package for reviewers", "open_questions": []},
    )
    session.add(analysis)
    session.flush()
    review = ensure_review_session(session, opportunity_id=opp.id)
    review.ai_decision_package_id = analysis.id
    review.status = "ready_for_review"
    session.flush()
    return review


def _review(session: Session, opportunity_id: int) -> ReviewSession:
    row = session.scalar(
        select(ReviewSession).where(ReviewSession.opportunity_id == opportunity_id)
    )
    assert row is not None
    return row


def test_two_reviewers_share_workspace_parallel_with_attributed_comments(session) -> None:
    opp = _opp(session)
    reviewer_a = _user(session, "reviewer", "a")
    reviewer_b = _user(session, "reviewer", "b")
    assign_reviewer(session, opportunity_id=opp.id, user_id=reviewer_a.id)
    assign_reviewer(session, opportunity_id=opp.id, user_id=reviewer_b.id)
    _attach_decision_package(session, opp)

    workspace_a = review_workspace(session, opportunity_id=opp.id, user_id=reviewer_a.id)
    workspace_b = review_workspace(session, opportunity_id=opp.id, user_id=reviewer_b.id)
    assert workspace_a["decision_package"] == workspace_b["decision_package"]
    assert len(workspace_a["assignments"]) == 2
    assert len(workspace_b["assignments"]) == 2

    add_comment(
        session,
        opportunity_id=opp.id,
        user_id=reviewer_a.id,
        topic="delivery",
        body="Delivery window appears aggressive unless supplier confirms expedite.",
        validate_with_ai=False,
    )
    add_comment(
        session,
        opportunity_id=opp.id,
        user_id=reviewer_b.id,
        topic="pricing",
        body="Margin holds only if freight assumptions remain stable at current fuel levels.",
        validate_with_ai=False,
    )

    rows = session.scalars(
        select(ReviewComment)
        .where(ReviewComment.opportunity_id == opp.id)
        .order_by(ReviewComment.id)
    ).all()
    assert [r.user_id for r in rows] == [reviewer_a.id, reviewer_b.id]
    assert rows[0].created_at is not None
    assert rows[1].created_at is not None


def test_ai_validation_populates_side_opinion_without_changing_human_comment(session) -> None:
    opp = _opp(session)
    reviewer = _user(session, "reviewer", "solo")
    assign_reviewer(session, opportunity_id=opp.id, user_id=reviewer.id)
    _attach_decision_package(session, opp)

    with patch("govcon.ai.structured.get_provider", return_value=_FakeProvider()):
        row = add_comment(
            session,
            opportunity_id=opp.id,
            user_id=reviewer.id,
            topic="delivery",
            body="The proposed lead time is not supported by supplier evidence and appears optimistic.",
        )

    assert (
        row.body
        == "The proposed lead time is not supported by supplier evidence and appears optimistic."
    )
    assert row.ai_position == "disagree"
    assert row.ai_confidence == "medium"
    assert "conflicts with cited source evidence" in (row.ai_reason or "")
    assert isinstance(row.ai_supporting_evidence, list) and row.ai_supporting_evidence
    assert isinstance(row.ai_contradicting_evidence, list) and row.ai_contradicting_evidence
    assert row.ai_missing_information == ["Supplier expedited lead-time confirmation"]


def test_single_quorum_can_progress_without_waiting_for_optional_second_reviewer(session) -> None:
    opp = _opp(session)
    approver = _user(session, "approver", "ap")
    reviewer_a = _user(session, "reviewer", "a")
    reviewer_b = _user(session, "reviewer", "b")
    assign_reviewer(session, opportunity_id=opp.id, user_id=reviewer_a.id)
    assign_reviewer(session, opportunity_id=opp.id, user_id=reviewer_b.id)
    _attach_decision_package(session, opp)
    set_review_policy(
        session,
        opportunity_id=opp.id,
        review_policy="single",
        actor=approver,
    )

    add_comment(
        session,
        opportunity_id=opp.id,
        user_id=reviewer_a.id,
        topic="delivery",
        body="Delivery assumptions need confirmation before final sign-off.",
        validate_with_ai=False,
    )
    complete_assignment(
        session,
        opportunity_id=opp.id,
        user_id=reviewer_a.id,
        action="approve_continue",
        recommendation="bid",
    )

    review = _review(session, opp.id)
    assert review.required_review_count == 1
    assert review.completed_review_count == 1
    assert review.status in {"approval_pending", "review_complete"}
    pending = session.scalar(
        select(ReviewAssignment).where(
            ReviewAssignment.opportunity_id == opp.id,
            ReviewAssignment.user_id == reviewer_b.id,
        )
    )
    assert pending is not None and pending.status != "complete"

    approved = finalize_approval(
        session,
        opportunity_id=opp.id,
        actor=approver,
        action="approve_to_bid",
        expected_version=_review(session, opp.id).version,
    )
    assert approved.final_approval_status == "approved_to_bid"


def test_dual_quorum_blocks_approval_until_override_is_explicit(session) -> None:
    opp = _opp(session)
    approver = _user(session, "approver", "ap")
    reviewer_a = _user(session, "reviewer", "a")
    reviewer_b = _user(session, "reviewer", "b")
    assign_reviewer(session, opportunity_id=opp.id, user_id=reviewer_a.id)
    assign_reviewer(session, opportunity_id=opp.id, user_id=reviewer_b.id)
    _attach_decision_package(session, opp)
    set_review_policy(
        session,
        opportunity_id=opp.id,
        review_policy="dual",
        actor=approver,
    )

    add_comment(
        session,
        opportunity_id=opp.id,
        user_id=reviewer_a.id,
        topic="pricing",
        body="Price is acceptable if raw-material surcharge remains capped.",
        validate_with_ai=False,
    )
    complete_assignment(
        session,
        opportunity_id=opp.id,
        user_id=reviewer_a.id,
        action="approve_continue",
        recommendation="bid",
    )

    with pytest.raises(ReviewWorkflowError):
        finalize_approval(
            session,
            opportunity_id=opp.id,
            actor=approver,
            action="approve_to_bid",
            expected_version=_review(session, opp.id).version,
        )

    row = finalize_approval(
        session,
        opportunity_id=opp.id,
        actor=approver,
        action="approve_to_bid",
        expected_version=_review(session, opp.id).version,
        override_reason="Customer urgency requires immediate approval while second reviewer is unavailable.",
    )
    assert row.final_approval_status == "approved_to_bid"
    assert row.override_used is True
    assert row.override_by_user_id == approver.id


def test_reviewer_requested_second_review_trigger_is_honored_and_audited(session) -> None:
    opp = _opp(session)
    approver = _user(session, "approver", "ap")
    reviewer_a = _user(session, "reviewer", "a")
    reviewer_b = _user(session, "reviewer", "b")
    assign_reviewer(session, opportunity_id=opp.id, user_id=reviewer_a.id)
    assign_reviewer(session, opportunity_id=opp.id, user_id=reviewer_b.id)
    _attach_decision_package(session, opp)
    set_review_policy(
        session,
        opportunity_id=opp.id,
        review_policy="conditional",
        actor=approver,
    )
    add_comment(
        session,
        opportunity_id=opp.id,
        user_id=reviewer_a.id,
        topic="supplier",
        body="Supplier historical on-time profile is mixed and needs a second opinion.",
        validate_with_ai=False,
    )
    assignment = complete_assignment(
        session,
        opportunity_id=opp.id,
        user_id=reviewer_a.id,
        action="request_second_review",
        second_review_reason="conflicting supplier lead-time evidence",
    )

    review = _review(session, opp.id)
    assert review.required_review_count == 2
    assert review.second_review_required is True
    assert "reviewer_requested_second_review" in (review.second_review_reason or "")

    event = session.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.action_type == "review_assignment_completed",
            AuditEvent.entity_id == assignment.id,
        )
        .order_by(AuditEvent.id.desc())
    )
    assert event is not None
    assert bool((event.new_value or {}).get("second_review_requested")) is True


def test_consolidated_review_contains_alignment_disagreements_open_issues_and_evidence(session) -> None:
    opp = _opp(session)
    approver = _user(session, "approver", "ap")
    reviewer_a = _user(session, "reviewer", "a")
    reviewer_b = _user(session, "reviewer", "b")
    assign_reviewer(session, opportunity_id=opp.id, user_id=reviewer_a.id)
    assign_reviewer(session, opportunity_id=opp.id, user_id=reviewer_b.id)
    _attach_decision_package(session, opp)
    set_review_policy(
        session,
        opportunity_id=opp.id,
        review_policy="dual",
        actor=approver,
    )

    with patch("govcon.ai.structured.get_provider", return_value=_FakeProvider()):
        add_comment(
            session,
            opportunity_id=opp.id,
            user_id=reviewer_a.id,
            topic="delivery",
            body="Delivery risk appears understated in the package due to supplier constraints.",
        )
        add_comment(
            session,
            opportunity_id=opp.id,
            user_id=reviewer_b.id,
            topic="supplier",
            body="Current supplier history suggests a higher schedule risk than package assumptions.",
        )

    complete_assignment(
        session,
        opportunity_id=opp.id,
        user_id=reviewer_a.id,
        action="approve_continue",
        recommendation="bid",
    )
    complete_assignment(
        session,
        opportunity_id=opp.id,
        user_id=reviewer_b.id,
        action="approve_continue",
        recommendation="no_bid",
    )

    review = _review(session, opp.id)
    consolidated = review.ai_consolidated_review or {}
    assert consolidated.get("reviewer_alignment") in {
        "single_reviewer",
        "aligned",
        "mixed",
        "conflicting",
    }
    assert "shared_concerns" in consolidated
    assert "disagreements" in consolidated
    assert "open_questions" in consolidated
    assert "evidence_needed_before_approval" in consolidated
    assert consolidated.get("human_approval_required") is True


def test_approved_to_bid_requires_authorized_human_action(session) -> None:
    opp = _opp(session)
    reviewer = _user(session, "reviewer", "solo")
    assign_reviewer(session, opportunity_id=opp.id, user_id=reviewer.id)
    _attach_decision_package(session, opp)

    add_comment(
        session,
        opportunity_id=opp.id,
        user_id=reviewer.id,
        topic="delivery",
        body="Schedule appears acceptable with existing evidence.",
        validate_with_ai=False,
    )
    complete_assignment(
        session,
        opportunity_id=opp.id,
        user_id=reviewer.id,
        action="approve_continue",
        recommendation="bid",
    )

    with pytest.raises(PermissionDenied):
        finalize_approval(
            session,
            opportunity_id=opp.id,
            actor=reviewer,
            action="approve_to_bid",
            expected_version=_review(session, opp.id).version,
        )


def test_material_amendment_reopens_completed_reviews(session) -> None:
    opp = _opp(session)
    approver = _user(session, "approver", "ap")
    reviewer_a = _user(session, "reviewer", "a")
    reviewer_b = _user(session, "reviewer", "b")
    assign_reviewer(session, opportunity_id=opp.id, user_id=reviewer_a.id)
    assign_reviewer(session, opportunity_id=opp.id, user_id=reviewer_b.id)
    _attach_decision_package(session, opp)
    set_review_policy(
        session,
        opportunity_id=opp.id,
        review_policy="dual",
        actor=approver,
    )

    add_comment(
        session,
        opportunity_id=opp.id,
        user_id=reviewer_a.id,
        topic="delivery",
        body="Delivery assumptions need supplier reconfirmation after latest amendment notice.",
        validate_with_ai=False,
    )
    add_comment(
        session,
        opportunity_id=opp.id,
        user_id=reviewer_b.id,
        topic="compliance",
        body="Compliance matrix changed materially versus prior amendment baseline.",
        validate_with_ai=False,
    )
    complete_assignment(
        session,
        opportunity_id=opp.id,
        user_id=reviewer_a.id,
        action="approve_continue",
        recommendation="bid",
    )
    complete_assignment(
        session,
        opportunity_id=opp.id,
        user_id=reviewer_b.id,
        action="approve_continue",
        recommendation="bid",
    )

    finding = ComplianceFinding(
        opportunity_id=opp.id,
        finding_type="review_reopen_required",
        severity="high",
        description="Material amendment changed requirement language after review completion.",
        detected_by="amendment_analysis",
        certainty="high",
    )
    session.add(finding)
    session.flush()

    changed = apply_material_amendment_reopen(
        session,
        opportunity_id=opp.id,
        actor=approver,
    )
    assert changed is True

    assignments = session.scalars(
        select(ReviewAssignment).where(ReviewAssignment.opportunity_id == opp.id)
    ).all()
    assert assignments
    assert all(row.status == "reopened" for row in assignments)
    assert all(row.completed_at is None for row in assignments)

    review = _review(session, opp.id)
    assert review.status == "under_review"
    assert review.final_approval_status is None
    assert review.second_review_required is True
    assert "material_amendment" in (review.second_review_reason or "")


def test_comment_validation_award_context_includes_vendor_names(session) -> None:
    """H4: the award evidence sent with a comment no longer crashes on PricePoint."""
    from decimal import Decimal

    from govcon.collaboration.ai_comment_review import _award_payload
    from govcon.models import Award

    opp = _opp(session)
    session.add(
        Award(
            source="usaspending",
            award_id=f"phase10-award-{uuid4().hex}",
            psc_code=opp.psc_code,
            recipient_name="Comparable Vendor LLC",
            total_obligation=Decimal(1500),
            raw={"fixture": True},
        )
    )
    session.flush()
    payload = _award_payload(session, opportunity_id=opp.id)
    assert any(item["vendor"] == "Comparable Vendor LLC" for item in payload["comparables"])
