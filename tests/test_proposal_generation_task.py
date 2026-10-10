"""Roadmap §9: bid approval → durable proposal task → visible progress/failure → protected retry.

Each test names the acceptance criterion (AC1–AC8) it proves. Generation runs
through the real worker against the temporary PostgreSQL database; with no AI
provider configured the worker builds the placeholder draft, so no live model
is called.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
import test_web_ui
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from test_web_ui import _make_user
from web_client import CsrfTestClient

from govcon.models import (
    AuditEvent,
    Opportunity,
    Proposal,
    ProposalVersion,
    Pursuit,
    ReviewSession,
    Submission,
    Task,
)
from govcon.tasks import queue
from govcon.tasks.testing import drain
from govcon.web.app import create_app
from govcon.workflow.proposal_generation import PROPOSAL_TASK


@pytest.fixture()
def db(upgraded_engine):
    with Session(upgraded_engine) as session:
        yield session


@pytest.fixture()
def client(upgraded_engine):
    with CsrfTestClient(create_app(), follow_redirects=False) as client:
        yield client


def approved_bid(db, client, *, approve=True):
    """An opportunity whose review is ready for approval (approved when ``approve``)."""
    opp, pursuit, review, actor, token = test_web_ui.TestApproveToGenerates()._make_opp_for_generate(db, uuid4().hex[:8])
    client.cookies.set("govcon_session", token)
    if approve:
        response = client.post(f"/workspace/{opp.id}/approve", data={
            "decision": "approve_to_bid", "expected_version": review.version})
        assert response.status_code == 303 and "error=" not in response.headers["location"]
        db.expire_all()
        review = db.get(ReviewSession, review.id)
    return opp, pursuit, review, actor


def tasks_for(db, opp_id):
    db.expire_all()
    return db.scalars(select(Task).where(Task.opportunity_id == opp_id).order_by(Task.id)).all()


def counts(db, opp_id):
    db.expire_all()
    proposals = db.scalars(select(Proposal.id).where(Proposal.opportunity_id == opp_id)).all()
    versions = db.scalar(select(func.count()).select_from(ProposalVersion).where(
        ProposalVersion.proposal_id.in_(proposals))) if proposals else 0
    submissions = db.scalar(select(func.count()).select_from(Submission).where(Submission.opportunity_id == opp_id))
    return len(proposals), versions, submissions


def audit_actions(db, opp_id):
    db.expire_all()
    return [e.action_type for e in db.scalars(
        select(AuditEvent).where(AuditEvent.opportunity_id == opp_id).order_by(AuditEvent.id))]


def make_due(db, task_id):
    db.execute(update(Task).where(Task.id == task_id).values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1)))
    db.commit()


# ── AC1: approval and task commit together; the response does not wait ──────

def test_ac1_approval_queues_generation_and_returns_immediately(db, client):
    opp, pursuit, review, _ = approved_bid(db, client)
    assert review.status == review.final_approval_status == "approved_to_bid"
    [task] = tasks_for(db, opp.id)
    assert (task.task_type, task.status) == (PROPOSAL_TASK, "queued")
    assert task.input_revision["source_revision"] and task.input_revision["review_id"] == review.id
    assert counts(db, opp.id) == (0, 0, 0), "approval must not generate inside the request"

    assert drain(opportunity_id=opp.id) == [(task.id, "succeeded")]
    assert counts(db, opp.id) == (1, 1, 1)
    task = tasks_for(db, opp.id)[0]
    assert task.result["proposal_id"] and task.result["provider"] == "placeholder"
    assert db.get(Pursuit, pursuit.id).stage == "bid_approved"


def test_ac1_failure_to_queue_rolls_back_the_approval(db, client, monkeypatch):
    opp, _, review, _ = approved_bid(db, client, approve=False)
    real_enqueue = queue.enqueue

    def enqueue_then_fail(*args, **kwargs):
        real_enqueue(*args, **kwargs)
        raise RuntimeError("synthetic failure after enqueue")

    monkeypatch.setattr("govcon.workflow.proposal_generation.queue.enqueue", enqueue_then_fail)
    response = client.post(f"/workspace/{opp.id}/approve", data={
        "decision": "approve_to_bid", "expected_version": review.version})
    assert response.status_code == 303 and "error=" in response.headers["location"]
    db.expire_all()
    assert db.get(ReviewSession, review.id).status == "approval_pending"
    assert tasks_for(db, opp.id) == []


# ── AC2: the Proposal tab shows status, step, owner and next action ──────────

@pytest.mark.parametrize("status,expected,polls", [
    ("queued", "Proposal generation: Queued", True),
    ("running", "Step: generate", True),
    ("retrying", "Attempt 1 of 5 failed", True),
    ("waiting_for_budget", "Waiting for AI budget", False),
    ("waiting_for_input", "Next action (approver)", False),
    ("failed", "Proposal generation failed", False),
])
def test_ac2_status_fragment_renders_each_state(db, client, status, expected, polls):
    opp, _, _, _ = approved_bid(db, client)
    task = tasks_for(db, opp.id)[0]
    values = {"status": status, "current_step": "generate", "attempts": 1,
              "last_error": "synthetic outage", "blocker_owner_role": "approver",
              "blocker_next_action": "Do the next thing."}
    if status in ("failed", "waiting_for_input", "waiting_for_budget"):
        values["finished_at"] = datetime.now(UTC)
    db.execute(update(Task).where(Task.id == task.id).values(**values))
    db.commit()
    fragment = client.get(f"/workspace/{opp.id}/proposal/status")
    assert fragment.status_code == 200
    assert expected in fragment.text
    assert ('hx-trigger="every 5s"' in fragment.text) is polls
    page = client.get(f"/workspace/{opp.id}?tab=proposal")
    assert expected in page.text
    retry_form = f'action="/workspace/{opp.id}/proposal/retry"' in page.text
    assert retry_form is (not polls), "retry is offered only when nothing is queued or running"


def test_ac2_status_fragment_requires_login(client, db):
    opp, _, _, _ = approved_bid(db, client)
    client.cookies.clear()
    assert client.get(f"/workspace/{opp.id}/proposal/status").status_code == 401


# ── AC3: a failed publish keeps the approval and leaves no half artifacts ────

def test_ac3_failed_publish_rolls_back_artifacts_and_retries(db, client, monkeypatch):
    opp, pursuit, review, _ = approved_bid(db, client)

    def fail_package(*args, **kwargs):
        raise RuntimeError("package store unavailable")

    monkeypatch.setattr("govcon.submissions.service.generate_submission_package", fail_package)
    [(task_id, status)] = drain(opportunity_id=opp.id)
    assert status == "retrying"
    assert counts(db, opp.id) == (0, 0, 0)
    db.expire_all()
    assert db.get(ReviewSession, review.id).status == "approved_to_bid"
    assert db.get(Pursuit, pursuit.id).stage == "bid_approved"
    task = db.get(Task, task_id)
    assert task.last_error == "package store unavailable" and task.attempts == 1

    monkeypatch.undo()
    make_due(db, task_id)
    assert drain(opportunity_id=opp.id) == [(task_id, "succeeded")]
    assert counts(db, opp.id) == (1, 1, 1)


def test_ac3_exhausted_retries_fail_with_owner_and_next_action(db, client, monkeypatch):
    opp, _, _review, _ = approved_bid(db, client)
    task = tasks_for(db, opp.id)[0]
    db.execute(update(Task).where(Task.id == task.id).values(max_attempts=1))
    db.commit()
    monkeypatch.setattr("govcon.submissions.service.generate_submission_package",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("upstream api_key=sk-live-123456")))
    assert drain(opportunity_id=opp.id) == [(task.id, "failed")]
    task = tasks_for(db, opp.id)[0]
    assert task.blocker_owner_role == "owner" and task.blocker_next_action
    assert "sk-live-123456" not in (task.last_error or ""), "errors are redacted before storage"
    assert "task_failed" in audit_actions(db, opp.id)
    page = client.get(f"/workspace/{opp.id}?tab=proposal")
    assert "Proposal generation failed" in page.text and "sk-live-123456" not in page.text
    assert f'action="/workspace/{opp.id}/proposal/retry"' in page.text
    ops = client.get("/ops").text
    assert f"#{task.id}" in ops and task.blocker_next_action in ops and "sk-live-123456" not in ops
    assert f'action="/ops/tasks/{task.id}/retry"' in ops


# ── AC4: protected retry ─────────────────────────────────────────────────────

def failed_generation(db, client, monkeypatch):
    opp, pursuit, review, actor = approved_bid(db, client)
    task = tasks_for(db, opp.id)[0]
    db.execute(update(Task).where(Task.id == task.id).values(max_attempts=1))
    db.commit()
    monkeypatch.setattr("govcon.submissions.service.generate_submission_package",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("synthetic outage")))
    assert drain(opportunity_id=opp.id) == [(task.id, "failed")]
    monkeypatch.undo()
    return opp, pursuit, review, actor


def test_ac4_retry_requeues_and_generates_once(db, client, monkeypatch):
    opp, _, review, actor = failed_generation(db, client, monkeypatch)
    response = client.post(f"/workspace/{opp.id}/proposal/retry", data={"expected_version": review.version})
    assert response.status_code == 303 and "notice=" in response.headers["location"]
    retried = db.scalar(select(AuditEvent).where(
        AuditEvent.opportunity_id == opp.id, AuditEvent.action_type == "proposal_generation_retried"))
    assert retried.user_id == actor.id and retried.new_value["outcome"] == "queued"
    again = client.post(f"/workspace/{opp.id}/proposal/retry", data={"expected_version": review.version})
    assert "already queued" in client.get(again.headers["location"]).text
    drain(opportunity_id=opp.id)
    assert counts(db, opp.id) == (1, 1, 1)
    statuses = [t.status for t in tasks_for(db, opp.id)]
    assert statuses == ["failed", "succeeded"]
    after = client.post(f"/workspace/{opp.id}/proposal/retry", data={"expected_version": review.version})
    assert "error=" in after.headers["location"], "an existing proposal is never regenerated"
    assert counts(db, opp.id) == (1, 1, 1)


def test_ac4_retry_without_ai_uses_the_placeholder_path(db, client, monkeypatch):
    opp, _, review, _ = failed_generation(db, client, monkeypatch)
    client.post(f"/workspace/{opp.id}/proposal/retry", data={"expected_version": review.version, "without_ai": "1"})
    task = tasks_for(db, opp.id)[-1]
    assert task.payload == {"without_ai": True} and task.input_revision["without_ai"] is True
    drain(opportunity_id=opp.id)
    assert counts(db, opp.id) == (1, 1, 1)


@pytest.mark.parametrize("problem", ["stale_version", "missing_version", "reviewer", "not_approved", "stale_package"])
def test_ac4_retry_refusals_change_nothing(db, client, monkeypatch, problem):
    opp, _, review, _ = failed_generation(db, client, monkeypatch)
    data = {"expected_version": review.version}
    if problem == "stale_version":
        data["expected_version"] = review.version - 1
    elif problem == "missing_version":
        data = {}
    elif problem == "reviewer":
        _, token = _make_user(db, f"retry-reviewer-{uuid4().hex}@example.test", "reviewer")
        client.cookies.set("govcon_session", token)
        assert f'action="/workspace/{opp.id}/proposal/retry"' not in client.get(f"/workspace/{opp.id}?tab=proposal").text
    elif problem == "not_approved":
        returned = client.post(f"/workspace/{opp.id}/approve", data={
            "decision": "return_for_review", "expected_version": review.version})
        assert "error=" not in returned.headers["location"]
        db.expire_all()
        data["expected_version"] = db.get(ReviewSession, review.id).version
    elif problem == "stale_package":
        monkeypatch.setattr("govcon.web.routes.proposals.decision_package_is_stale", lambda *a, **k: True)
    before_audit, before_tasks = audit_actions(db, opp.id), [(t.id, t.status) for t in tasks_for(db, opp.id)]
    response = client.post(f"/workspace/{opp.id}/proposal/retry", data=data)
    assert response.status_code == 303 and "error=" in response.headers["location"]
    assert audit_actions(db, opp.id) == before_audit
    assert [(t.id, t.status) for t in tasks_for(db, opp.id)] == before_tasks
    assert counts(db, opp.id) == (0, 0, 0)


def test_ac4_a_task_waiting_for_input_is_requeued_not_duplicated(db, client):
    opp, _, review, _ = approved_bid(db, client)
    task = tasks_for(db, opp.id)[0]
    db.execute(update(Task).where(Task.id == task.id).values(
        status="waiting_for_input", blocker_owner_role="owner", blocker_next_action="Configure AI."))
    db.commit()
    response = client.post(f"/workspace/{opp.id}/proposal/retry", data={"expected_version": review.version})
    assert "notice=" in response.headers["location"]
    [task] = tasks_for(db, opp.id)
    assert task.status == "queued" and task.blocker_next_action is None
    drain(opportunity_id=opp.id)
    assert counts(db, opp.id) == (1, 1, 1)


# ── Blocked states: budget and policy are not silent placeholders ────────────

@pytest.mark.parametrize("reason,status", [("budget_exceeded", "waiting_for_budget"),
                                           ("blocked_by_policy", "waiting_for_input")])
def test_budget_and_policy_errors_block_with_an_owner(db, client, monkeypatch, reason, status):
    from govcon.ai.structured import StructuredCallError
    opp, _, _, _ = approved_bid(db, client)
    monkeypatch.setattr("govcon.ai.routing.analysis_available", lambda session, settings: True)

    def refuse(*args, **kwargs):
        raise StructuredCallError(reason, "synthetic refusal")

    monkeypatch.setattr("govcon.proposals.drafting.prepare_draft_call", refuse)
    [(_task_id, result)] = drain(opportunity_id=opp.id)
    assert result == status
    task = tasks_for(db, opp.id)[0]
    assert task.blocker_owner_role == "owner" and task.blocker_next_action
    assert task.attempts == 0, "a block is not a failed attempt"
    assert counts(db, opp.id) == (0, 0, 0)
    assert "task_blocked" in audit_actions(db, opp.id)


# ── AC5: a worker killed mid-step loses nothing; its late result is refused ──

def test_ac5_killed_worker_is_resumed_by_another(db, client, tmp_path):
    opp, _, _, _ = approved_bid(db, client)
    [task] = tasks_for(db, opp.id)
    script = tmp_path / "hanging_worker.py"
    script.write_text(textwrap.dedent(f"""
        import time
        from govcon.tasks.registry import get_handler
        from govcon.tasks.worker import run_once

        def hang(draft, ctx):
            time.sleep(600)

        get_handler("proposal_generation").steps[0].execute = hang
        run_once(opportunity_id={opp.id})
    """), encoding="utf-8")
    env = dict(os.environ, TASK_LEASE_SECONDS="2", PYTHONPATH=os.pathsep.join(sys.path))
    worker = subprocess.Popen([sys.executable, str(script)], env=env)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            db.expire_all()
            current = db.get(Task, task.id)
            if current.status == "running" and current.current_step == "generate":
                break
            time.sleep(0.2)
        else:
            pytest.fail("worker never started the generate step")
        time.sleep(0.5)  # inside execute, no transaction open
    finally:
        worker.kill()
        worker.wait(timeout=30)
    assert counts(db, opp.id) == (0, 0, 0), "nothing is half-published by a dead worker"
    time.sleep(2.5)  # the dead worker's lease lapses
    assert drain(opportunity_id=opp.id) == [(task.id, "succeeded")]
    db.expire_all()
    finished = db.get(Task, task.id)
    assert finished.claim_token == 2 and finished.attempts == 2
    assert counts(db, opp.id) == (1, 1, 1)


def test_ac5_late_publish_from_an_expired_lease_is_refused(db, client):
    from govcon.db import session_scope
    opp, _, _, _ = approved_bid(db, client)
    [task] = tasks_for(db, opp.id)
    with session_scope() as s:
        stale = queue.claim(s, worker_id="dead-worker", lease_seconds=1, opportunity_id=opp.id)
    assert stale is not None and stale.task_id == task.id
    time.sleep(1.2)
    assert drain(opportunity_id=opp.id) == [(task.id, "succeeded")]
    with pytest.raises(queue.LeaseLost), session_scope() as s:
        queue.guard_publish(s, stale)
    assert counts(db, opp.id) == (1, 1, 1)


# ── AC6: concurrent work produces exactly one result ─────────────────────────

def test_ac6_two_workers_racing_produce_one_proposal(db, client):
    from govcon.tasks.worker import run_once
    opp, _, _, _ = approved_bid(db, client)
    barrier = Barrier(2)

    def work():
        barrier.wait(timeout=10)
        return run_once(opportunity_id=opp.id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result(timeout=120) for f in [pool.submit(work) for _ in range(2)]]
    assert min(results, key=lambda r: r is None)[1] == "succeeded"
    assert sum(r is None for r in results) == 1, "SKIP LOCKED hands the task to one worker"
    assert counts(db, opp.id) == (1, 1, 1)


def test_ac6_concurrent_retries_queue_one_task(db, client, monkeypatch):
    opp, _, review, _ = failed_generation(db, client, monkeypatch)
    token = client.cookies.get("govcon_session")
    barrier = Barrier(2)

    def retry():
        with CsrfTestClient(create_app(), follow_redirects=False) as other:
            other.cookies.set("govcon_session", token)
            barrier.wait(timeout=10)
            return other.post(f"/workspace/{opp.id}/proposal/retry", data={"expected_version": review.version})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = [f.result(timeout=60) for f in [pool.submit(retry) for _ in range(2)]]
    assert sum("error=" in r.headers["location"] for r in responses) == 1
    assert [t.status for t in tasks_for(db, opp.id)] == ["failed", "queued"]
    drain(opportunity_id=opp.id)
    assert counts(db, opp.id) == (1, 1, 1)


def test_ac6_identical_work_is_queued_once(db, client):
    from govcon.db import session_scope
    from govcon.workflow.proposal_generation import queue_proposal_generation
    opp, _, _, _ = approved_bid(db, client)
    with session_scope() as s:
        _task, created = queue_proposal_generation(s, opportunity_id=opp.id, actor_user_id=None)
    assert created is False and len(tasks_for(db, opp.id)) == 1


# ── AC7: an amendment during generation rejects the stale result ─────────────

def _amend_during_execute(monkeypatch, opp_id):
    from govcon.db import session_scope
    from govcon.tasks.registry import get_handler
    step = get_handler(PROPOSAL_TASK).steps[0]
    original = step.execute

    def amend_then_execute(draft, ctx):
        with session_scope() as s:
            s.execute(update(Opportunity).where(Opportunity.id == opp_id).values(raw_hash=uuid4().hex))
        return original(draft, ctx)

    monkeypatch.setattr(step, "execute", amend_then_execute)


def test_ac7_amendment_during_generation_supersedes_the_task(db, client, monkeypatch):
    opp, _, _, _ = approved_bid(db, client)
    _amend_during_execute(monkeypatch, opp.id)
    first = drain(opportunity_id=opp.id, max_tasks=1)
    assert first[0][1] == "cancelled"
    assert counts(db, opp.id) == (0, 0, 0), "the stale result is never published"
    old, new = tasks_for(db, opp.id)
    assert old.superseded_by_task_id == new.id and new.status == "queued"
    assert new.input_revision["source_revision"] != old.input_revision["source_revision"]
    monkeypatch.undo()
    assert drain(opportunity_id=opp.id) == [(new.id, "succeeded")]
    assert counts(db, opp.id) == (1, 1, 1)


def test_ac7_amendment_with_a_stale_package_waits_for_the_approver(db, client, monkeypatch):
    opp, _, _, _ = approved_bid(db, client)
    monkeypatch.setattr("govcon.collaboration.review_sessions.decision_package_is_stale", lambda *a, **k: True)
    assert drain(opportunity_id=opp.id)[0][1] == "waiting_for_input"
    task = tasks_for(db, opp.id)[0]
    assert task.blocker_owner_role == "approver" and "decision package" in task.blocker_next_action
    assert counts(db, opp.id) == (0, 0, 0)


def test_ac7_material_source_change_cancels_inflight_generation(db, client):
    from govcon.db import session_scope
    from govcon.workflow.invalidation import apply_source_change, lock_opportunity
    opp, _, _review, _ = approved_bid(db, client)
    with session_scope() as s:
        lock_opportunity(s, opp.id)
        apply_source_change(s, opp.id, level="material", reason="amendment 0001")
    [task] = tasks_for(db, opp.id)
    assert task.status == "cancelled" and "amendment 0001" in task.last_error
    assert drain(opportunity_id=opp.id) == []
    assert counts(db, opp.id) == (0, 0, 0)


# ── AC8: human edits and existing artifacts are never overwritten ────────────

def test_ac8_reapproval_never_regenerates_an_edited_proposal(db, client):
    from govcon.collaboration.review_sessions import finalize_approval
    from govcon.db import session_scope
    from govcon.models import User
    from govcon.proposals.versions import create_proposal_version
    opp, _, review, actor = approved_bid(db, client)
    drain(opportunity_id=opp.id)
    proposal = db.scalar(select(Proposal).where(Proposal.opportunity_id == opp.id))
    with session_scope() as s:
        create_proposal_version(s, proposal_id=proposal.id, created_by=actor.email,
                                sections=[{"key": "cover", "title": "Cover", "content": "Human edit"}],
                                change_summary="human edit", status_after="draft")
    assert counts(db, opp.id) == (1, 2, 1)
    with session_scope() as s:
        approver = s.get(User, actor.id)
        version = s.get(ReviewSession, review.id).version
        finalize_approval(s, opportunity_id=opp.id, actor=approver, action="return_for_review",
                          expected_version=version)
    with session_scope() as s:
        approver = s.get(User, actor.id)
        version = s.get(ReviewSession, review.id).version
        # Returning reopens the reviews, so re-approval needs an explicit override.
        finalize_approval(s, opportunity_id=opp.id, actor=approver, action="approve_to_bid",
                          expected_version=version, override_reason="Re-approved after a returned review")
    assert len(tasks_for(db, opp.id)) == 1, "re-approval queues nothing when a proposal exists"
    assert counts(db, opp.id) == (1, 2, 1)


def test_ac8_a_proposal_created_meanwhile_cancels_the_task(db, client):
    from govcon.db import session_scope
    from govcon.proposals.service import generate_proposal
    opp, _, _, _actor = approved_bid(db, client)
    with session_scope() as s:
        generate_proposal(s, opportunity_id=opp.id, skip_ai=True)
    assert drain(opportunity_id=opp.id)[0][1] == "cancelled"
    assert counts(db, opp.id) == (1, 1, 0)
    assert "already exists" in tasks_for(db, opp.id)[0].last_error
