"""Automatic review setup preserves the current human review cycle."""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from test_preparation import new_opportunity
from test_web_ui import _make_user

from govcon.collaboration.review_sessions import ensure_review_session
from govcon.models import ReviewAssignment
from govcon.tasks.worker import run_once
from govcon.workflow.app_settings import REVIEWER_ASSIGNMENT, set_setting
from govcon.workflow.preparation import STEPS, queue_preparation


def prepared_review(db, mode, status):
    opp = new_opportunity(db)
    owner, _ = _make_user(db, f"review-owner-{uuid4().hex}@example.test", "owner")
    reviewer, _ = _make_user(db, f"reviewer-{uuid4().hex}@example.test", "reviewer")
    review = ensure_review_session(db, opportunity_id=opp.id)
    review.status = "under_review"
    when = datetime.now(UTC) - timedelta(hours=2)
    assignment = ReviewAssignment(opportunity_id=opp.id, user_id=reviewer.id, status=status,
                                  started_at=when if status != "assigned" else None,
                                  completed_at=when if status == "complete" else None,
                                  reopened_at=when if status == "reopened" else None)
    db.add(assignment)
    set_setting(db, REVIEWER_ASSIGNMENT, {"mode": mode, "user_ids": [reviewer.id] if mode == "named" else []},
                actor=owner)
    task, _ = queue_preparation(db, opportunity_id=opp.id, actor_user_id=owner.id)
    task.checkpoint = {"completed_steps": list(STEPS[:-1])}
    db.commit()
    before = (assignment.status, assignment.started_at, assignment.completed_at, assignment.reopened_at)
    assert run_once(task_id=task.id) == (task.id, "succeeded")
    db.expire_all()
    return before, (assignment.status, assignment.started_at, assignment.completed_at, assignment.reopened_at)


@pytest.mark.parametrize("mode", ["named", "all_active"])
@pytest.mark.parametrize("status", ["assigned", "in_progress"])
def test_current_open_assignments_keep_their_progress(db, mode, status):
    before, after = prepared_review(db, mode, status)
    assert before == after


@pytest.mark.parametrize("mode", ["named", "all_active"])
@pytest.mark.parametrize("status", ["complete", "reopened"])
def test_automatic_setup_preserves_completed_and_reopened_review(db, mode, status):
    before, after = prepared_review(db, mode, status)
    assert before == after, "automatic preparation reset the human review cycle"
