"""Audit round 2, finding 3: assigning a reviewer who already finished keeps their review.

The Review tab, ``govcon review assign`` and the MCP ``assign_reviewer`` tool all
go through ``assign_reviewer``; a completed review must stay complete (with its
recommendation and completion time) and keep counting toward quorum.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_web_ui import _make_user
from web_client import CsrfTestClient

from govcon.collaboration.assignments import assign_reviewer
from govcon.collaboration.review_sessions import (
    complete_assignment,
    ensure_review_session,
    recalculate_quorum,
)
from govcon.models import (
    AuditEvent,
    Notification,
    Opportunity,
    ReviewAssignment,
    ReviewSession,
    User,
)
from govcon.web.app import create_app


@pytest.fixture()
def db(upgraded_engine):
    with Session(upgraded_engine) as session:
        yield session


def _opp(db: Session) -> Opportunity:
    opp = Opportunity(source="sam", source_id=f"r2-review-{uuid4().hex}", title="Reviewer re-assign fixture",
                      status="open", response_deadline=datetime.now(UTC) + timedelta(days=20), raw={}, links={})
    db.add(opp)
    db.flush()
    review = ensure_review_session(db, opportunity_id=opp.id)
    review.status = "ready_for_review"
    db.flush()
    return opp


def _user(db: Session, role: str = "reviewer") -> User:
    return _make_user(db, f"r2-{role}-{uuid4().hex[:10]}@example.test", role)[0]


def _completed(db: Session, opp: Opportunity, reviewer: User) -> ReviewAssignment:
    assign_reviewer(db, opportunity_id=opp.id, user_id=reviewer.id)
    row = complete_assignment(db, opportunity_id=opp.id, user_id=reviewer.id, action="approve_continue",
                              recommendation="bid", agree_with_ai_assessment=True)
    db.commit()
    assert row.status == "complete" and row.completed_at is not None
    return row


def _snapshot(db: Session, assignment_id: int):
    db.expire_all()
    row = db.get(ReviewAssignment, assignment_id)
    review = db.scalar(select(ReviewSession).where(ReviewSession.opportunity_id == row.opportunity_id))
    return (row.status, row.started_at, row.completed_at, row.recommendation, row.agree_with_ai_assessment), \
        review.completed_review_count


# ── current behaviour that must not change ───────────────────────────────────

def test_new_assignment_is_assigned_audited_and_notified(db):
    opp, reviewer, owner = _opp(db), _user(db), _user(db, "owner")
    row = assign_reviewer(db, opportunity_id=opp.id, user_id=reviewer.id, actor_user_id=owner.id)
    assert (row.status, row.assignment_role, row.started_at, row.completed_at) == ("assigned", "reviewer", None, None)
    audit = db.scalar(select(AuditEvent).where(AuditEvent.action_type == "review_assignment_upserted",
                                               AuditEvent.entity_id == row.id))
    assert audit.old_value is None and audit.new_value["status"] == "assigned" and audit.user_id == owner.id
    assert db.scalar(select(Notification).where(Notification.user_id == reviewer.id,
                                                Notification.notification_type == "review_assigned",
                                                Notification.opportunity_id == opp.id)) is not None
    db.rollback()


@pytest.mark.parametrize("status", ["assigned", "in_progress"])
def test_reassigning_an_open_assignment_keeps_its_progress_and_updates_the_role(db, status):
    opp, reviewer = _opp(db), _user(db)
    row = assign_reviewer(db, opportunity_id=opp.id, user_id=reviewer.id)
    started = datetime.now(UTC) - timedelta(hours=1) if status == "in_progress" else None
    row.status, row.started_at = status, started
    db.flush()
    again = assign_reviewer(db, opportunity_id=opp.id, user_id=reviewer.id, assignment_role="lead")
    assert again.id == row.id
    assert (again.status, again.started_at, again.assignment_role) == (status, started, "lead")
    db.rollback()


def test_reassigning_a_reopened_assignment_restarts_it(db):
    opp, reviewer = _opp(db), _user(db)
    row = assign_reviewer(db, opportunity_id=opp.id, user_id=reviewer.id)
    when = datetime.now(UTC) - timedelta(hours=2)
    row.status, row.started_at, row.completed_at, row.reopened_at = "reopened", when, None, when
    db.flush()
    again = assign_reviewer(db, opportunity_id=opp.id, user_id=reviewer.id)
    assert (again.status, again.started_at, again.completed_at) == ("assigned", None, None)
    db.rollback()


def test_inactive_user_cannot_be_assigned(db):
    opp, reviewer = _opp(db), _user(db)
    reviewer.is_active = False
    db.flush()
    with pytest.raises(ValueError):
        assign_reviewer(db, opportunity_id=opp.id, user_id=reviewer.id)
    db.rollback()


# ── the finding ──────────────────────────────────────────────────────────────

def test_reassigning_a_completed_reviewer_keeps_their_review_and_quorum(db):
    opp, reviewer = _opp(db), _user(db)
    row = _completed(db, opp, reviewer)
    recalculate_quorum(db, opportunity_id=opp.id)
    db.commit()
    before = _snapshot(db, row.id)
    assert before[1] == 1

    assign_reviewer(db, opportunity_id=opp.id, user_id=reviewer.id)
    recalculate_quorum(db, opportunity_id=opp.id)
    db.commit()
    assert _snapshot(db, row.id) == before, "re-assigning wiped a finished review"


def test_review_tab_assign_on_a_completed_reviewer_keeps_their_review(db):
    opp, reviewer = _opp(db), _user(db)
    _owner, token = _make_user(db, f"r2-owner-{uuid4().hex[:10]}@example.test", "owner")
    row = _completed(db, opp, reviewer)
    recalculate_quorum(db, opportunity_id=opp.id)
    db.commit()
    before = _snapshot(db, row.id)
    with CsrfTestClient(create_app(), follow_redirects=False) as client:
        client.cookies.set("govcon_session", token)
        response = client.post(f"/workspace/{opp.id}/assign", data={"user_id": str(reviewer.id)})
    assert response.status_code == 303 and "notice=" in response.headers["location"]
    assert _snapshot(db, row.id) == before


def test_mcp_assign_on_a_completed_reviewer_keeps_their_review(db, monkeypatch):
    from govcon.mcp import operations as ops
    from govcon.mcp.context import reset_server_actor

    opp, reviewer, approver = _opp(db), _user(db), _user(db, "approver")
    row = _completed(db, opp, reviewer)
    recalculate_quorum(db, opportunity_id=opp.id)
    db.commit()
    before = _snapshot(db, row.id)
    reset_server_actor()
    monkeypatch.setenv("MCP_ACTOR_EMAIL", approver.email)
    try:
        result = ops.op_assign_reviewer(db, opp.id, user_email=reviewer.email)
        db.commit()
    finally:
        reset_server_actor()
    assert result["data"]["status"] == "complete"
    assert _snapshot(db, row.id)[0] == before[0]
