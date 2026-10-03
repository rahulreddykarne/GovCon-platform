"""Preservation and regression checks for the executed feature audit findings."""

from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
import json
import asyncio
import os
import shutil
import subprocess
import re
import shlex
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from govcon.models import AuditEvent, Match, Opportunity, Pursuit, ReviewAssignment, ReviewSession, Watchlist
from govcon.web.app import create_app
from test_web_ui import _make_user
from web_client import CsrfTestClient


@pytest.fixture()
def db(upgraded_engine):
    with Session(upgraded_engine) as session:
        yield session


@pytest.fixture()
def client(upgraded_engine):
    with CsrfTestClient(create_app(), follow_redirects=False) as client:
        yield client


def matched_opportunity(db, client, *, role="reviewer", stage=None):
    actor, token = _make_user(db, f"feature-{uuid4().hex}@example.test", role)
    client.cookies.set("govcon_session", token)
    opp = Opportunity(source="sam", source_id=uuid4().hex, title=f"Feature {uuid4().hex}",
                      status="open", response_deadline=datetime.now(UTC) + timedelta(days=30), raw={})
    wl = Watchlist(name=f"Feature {uuid4().hex}", enabled=True)
    db.add_all([opp, wl])
    db.flush()
    match = Match(opportunity_id=opp.id, watchlist_id=wl.id, status="new", active=True)
    db.add(match)
    if stage:
        db.add(Pursuit(opportunity_id=opp.id, stage=stage, notes="Keep existing facts"))
    db.commit()
    return opp, match, actor, token


@pytest.mark.parametrize("action", ["seen", "dismissed", "reviewing"])
def test_f1_preserves_other_match_actions(db, client, action):
    opp, match, actor, _ = matched_opportunity(db, client)
    response = client.post("/inbox/action", data={"match_id": match.id, "action": action})
    assert response.status_code == 200 and response.content == b""
    db.expire_all()
    assert db.get(Match, match.id).status == action
    assert db.scalar(select(Pursuit).where(Pursuit.opportunity_id == opp.id)) is None
    event = db.scalar(select(AuditEvent).where(AuditEvent.opportunity_id == opp.id))
    assert event.action_type == "match_status_changed" and event.user_id == actor.id
    assert event.old_value == {"status": "new"} and event.new_value == {"status": action}


@pytest.mark.parametrize("stage", ["evaluating", "submitted"])
def test_f1_preserves_existing_pursuit(db, client, stage):
    opp, match, _, _ = matched_opportunity(db, client, stage=stage)
    original = db.scalar(select(Pursuit).where(Pursuit.opportunity_id == opp.id))
    original_id, original_version = original.id, original.version
    for _ in range(2):
        response = client.post("/inbox/action", data={"match_id": match.id, "action": "pursuing"})
        assert response.status_code == 200 and response.content == b""
    db.expire_all()
    pursuit = db.scalar(select(Pursuit).where(Pursuit.opportunity_id == opp.id))
    assert (pursuit.id, pursuit.version, pursuit.stage, pursuit.notes) == (
        original_id, original_version, stage, "Keep existing facts")
    assert db.scalar(select(func.count()).select_from(Pursuit).where(Pursuit.opportunity_id == opp.id)) == 1


@pytest.mark.parametrize("bad_request,code", [("invalid", 400), ("missing", 404), ("read_only", 403)])
def test_f1_preserves_triage_refusal(db, client, bad_request, code):
    opp, match, _, _ = matched_opportunity(db, client, role="read_only" if bad_request == "read_only" else "reviewer")
    response = client.post("/inbox/action", data={
        "match_id": -1 if bad_request == "missing" else match.id,
        "action": "invalid" if bad_request == "invalid" else "pursuing"})
    assert response.status_code == code
    db.expire_all()
    assert db.get(Match, match.id).status == "new"
    assert db.scalar(select(Pursuit).where(Pursuit.opportunity_id == opp.id)) is None


def test_f1_pursue_creates_one_visible_workspace(db, client):
    opp, match, actor, _ = matched_opportunity(db, client)
    for _ in range(2):
        response = client.post("/inbox/action", data={"match_id": match.id, "action": "pursuing"})
        assert response.status_code == 200 and response.content == b""
    db.expire_all()
    pursuits = db.scalars(select(Pursuit).where(Pursuit.opportunity_id == opp.id)).all()
    assert len(pursuits) == 1, "Pursue must create a workspace instead of hiding the opportunity"
    assert pursuits[0].stage == "evaluating"
    assert opp.title in client.get("/pipeline").text
    assert db.get(Match, match.id).status == "pursuing"
    events = db.scalars(select(AuditEvent).where(
        AuditEvent.opportunity_id == opp.id, AuditEvent.action_type == "pursuit_created")).all()
    assert len(events) == 1 and events[0].user_id == actor.id


@pytest.mark.parametrize("role", ["reviewer", "read_only"])
def test_f2_preserves_review_start_permissions_and_read_only_get(db, client, role):
    opp, _, _, _ = matched_opportunity(db, client, role=role, stage="evaluating")
    response = client.get(f"/workspace/{opp.id}?tab=review")
    assert response.status_code == 200
    assert f'action="/workspace/{opp.id}/assign"' not in response.text
    assert db.scalar(select(ReviewSession).where(ReviewSession.opportunity_id == opp.id)) is None


def test_f2_preserves_existing_assignment_and_review_controls(db, client):
    opp, _, actor, _ = matched_opportunity(db, client, role="approver", stage="evaluating")
    from govcon.collaboration.review_sessions import ensure_review_session
    from govcon.collaboration.assignments import assign_reviewer
    ensure_review_session(db, opportunity_id=opp.id)
    assign_reviewer(db, opportunity_id=opp.id, user_id=actor.id)
    db.commit()
    page = client.get(f"/workspace/{opp.id}?tab=review")
    assert page.status_code == 200
    for action in ("assign", "approve", "comment", "complete-review"):
        assert f'action="/workspace/{opp.id}/{action}"' in page.text
    assert actor.display_name in page.text and "Reviews: 0/" in page.text
    original = db.scalar(select(ReviewSession).where(ReviewSession.opportunity_id == opp.id))
    original_id, original_version = original.id, original.version
    response = client.post(f"/workspace/{opp.id}/assign", data={"user_id": actor.id})
    assert response.status_code == 303 and "notice=" in response.headers["location"]
    db.expire_all()
    current = db.get(ReviewSession, original_id)
    assert current.version == original_version and current.completed_review_count == 0
    assert db.scalar(select(func.count()).select_from(ReviewAssignment).where(
        ReviewAssignment.opportunity_id == opp.id)) == 1


def test_f2_empty_review_can_assign_first_reviewer(db, client):
    opp, _, actor, _ = matched_opportunity(db, client, role="approver", stage="evaluating")
    page = client.get(f"/workspace/{opp.id}?tab=review")
    assert f'action="/workspace/{opp.id}/assign"' in page.text, "Empty reviews need a reachable assignment form"
    assert 'name="csrf_token"' in page.text and f'value="{actor.id}"' in page.text
    assert db.scalar(select(ReviewSession).where(ReviewSession.opportunity_id == opp.id)) is None
    response = client.post(f"/workspace/{opp.id}/assign", data={"user_id": actor.id})
    assert response.status_code == 303 and "notice=" in response.headers["location"]
    db.expire_all()
    review = db.scalar(select(ReviewSession).where(ReviewSession.opportunity_id == opp.id))
    assert review is not None and review.status == "under_review"
    assignment = db.scalar(select(ReviewAssignment).where(ReviewAssignment.opportunity_id == opp.id))
    assert assignment.user_id == actor.id and assignment.status == "assigned"
    assert "My Review Actions" in client.get(f"/workspace/{opp.id}?tab=review").text


def approval_fixture(db, client):
    from test_web_ui import TestApproveToGenerates
    opp, pursuit, review, actor, token = TestApproveToGenerates()._make_opp_for_generate(db, uuid4().hex)
    client.cookies.set("govcon_session", token)
    return opp, pursuit, review, actor


def test_f3_preserves_approval_and_rolls_back_half_generated_artifacts(db, client, monkeypatch):
    from govcon.models import Proposal, Submission
    opp, pursuit, review, actor = approval_fixture(db, client)
    def fail_package(*args, **kwargs):
        raise RuntimeError("synthetic package failure")
    monkeypatch.setattr("govcon.submissions.service.generate_submission_package", fail_package)
    response = client.post(f"/workspace/{opp.id}/approve", data={
        "decision": "approve_to_bid", "expected_version": review.version})
    assert response.status_code == 303
    db.expire_all()
    assert db.get(ReviewSession, review.id).status == "approved_to_bid"
    assert db.get(Pursuit, pursuit.id).stage == "bid_approved"
    assert db.scalar(select(Proposal).where(Proposal.opportunity_id == opp.id)) is None
    assert db.scalar(select(Submission).where(Submission.opportunity_id == opp.id)) is None


@pytest.mark.xfail(strict=True, reason="F3 API/response change awaits the user's required approval")
def test_f3_failed_generation_is_honest_and_recoverable(db, client, monkeypatch):
    opp, _, review, _ = approval_fixture(db, client)
    def fail_generation(*args, **kwargs):
        raise RuntimeError("synthetic provider outage")
    monkeypatch.setattr("govcon.proposals.service.generate_proposal", fail_generation)
    response = client.post(f"/workspace/{opp.id}/approve", data={
        "decision": "approve_to_bid", "expected_version": review.version})
    assert response.status_code == 303
    assert "error=" in response.headers["location"], "Approval must report a failed proposal generation"
    page = client.get(f"/workspace/{opp.id}?tab=proposal")
    assert "generation failed" in page.text.lower()
    assert f'action="/workspace/{opp.id}/proposal/retry"' in page.text
    assert "generation in progress" not in page.text.lower()


def ready_submission(db, client, tmp_path):
    from test_release_lifecycle import prepared_lifecycle
    from govcon.collaboration.users import create_session
    from govcon.compliance.submission_preflight import run_submission_preflight
    from govcon.models import Proposal, Submission, User
    from govcon.proposals.service import finalize_proposal
    ids, package, configured = prepared_lifecycle(db.get_bind(), tmp_path)
    opp_id, actor_id, proposal_id, submission_id = ids
    assert run_submission_preflight(db, opp_id, package, use_ai=False, settings=configured)["ready"]
    actor = db.get(User, actor_id)
    proposal = db.get(Proposal, proposal_id)
    finalize_proposal(db, opportunity_id=opp_id, action="APPROVE_FOR_SUBMISSION", actor=actor,
                      expected_version=proposal.version)
    token = create_session(db, actor)
    db.commit()
    client.cookies.set("govcon_session", token)
    return db.get(Opportunity, opp_id), actor, db.get(Submission, submission_id)


@pytest.mark.parametrize("when", ["", "2026-01-15T12:00:00+00:00", "2026-07-15T12:00:00-05:00", "2026-01-15T12:00"])
def test_f6_preserves_existing_submission_time_inputs(db, client, tmp_path, when):
    opp, _, sub = ready_submission(db, client, tmp_path)
    before = datetime.now(UTC)
    page = client.get(f"/workspace/{opp.id}?tab=submission")
    assert f'action="/workspace/{opp.id}/submission/approve"' in page.text
    response = client.post(f"/workspace/{opp.id}/submission/approve", data={
        "expected_version": sub.version, "confirmation_number": "SYNTHETIC-TIME", "submitted_at": when})
    assert response.status_code == 303 and "notice=" in response.headers["location"]
    db.refresh(sub)
    expected = datetime.fromisoformat(when) if when else None
    if expected and expected.tzinfo is None:
        expected = expected.replace(tzinfo=UTC)
    assert sub.status == "submitted"
    assert sub.submitted_at == expected if expected else before <= sub.submitted_at <= datetime.now(UTC)


class Forms(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.forms = []
        self.current = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        if tag == "form":
            self.current = {"attrs": dict(attrs), "fields": []}
            self.forms.append(self.current)
        elif self.current is not None and tag == "input":
            self.current["fields"].append(dict(attrs))

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


@pytest.mark.parametrize("local_time,utc_time", [
    ("2026-01-15T12:00", "2026-01-15T18:00:00.000Z"),
    ("2026-07-15T12:00", "2026-07-15T17:00:00.000Z"),
])
def test_f6_browser_local_time_records_correct_instant(db, client, tmp_path, local_time, utc_time):
    opp, _, sub = ready_submission(db, client, tmp_path)
    page = client.get(f"/workspace/{opp.id}?tab=submission")
    form = next(f for f in Forms(page.text).forms if f["attrs"].get("action") ==
                f"/workspace/{opp.id}/submission/approve")
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable; rendered browser datetime handler cannot be executed")
    script = "const form = {elements: {submitted_at: {type: 'datetime-local', value:" + json.dumps(local_time) + "}}};"
    script += "new Function(" + json.dumps(form["attrs"].get("onsubmit", "")) + ").call(form);"
    script += "process.stdout.write(JSON.stringify(form.elements.submitted_at));"
    result = subprocess.run([node, "-e", script], env={**os.environ, "TZ": "America/Chicago"},
                            capture_output=True, text=True, check=True)
    field = json.loads(result.stdout)
    assert field["value"] == utc_time, "The browser must send an explicit instant, not a naive local datetime"
    assert field["type"] == "hidden", "ISO UTC cannot be held in a datetime-local input"
    response = client.post(form["attrs"]["action"], data={
        "expected_version": sub.version, "confirmation_number": "SYNTHETIC-LOCAL", "submitted_at": field["value"]})
    assert response.status_code == 303 and "notice=" in response.headers["location"]
    db.refresh(sub)
    assert sub.submitted_at == datetime.fromisoformat(utc_time)


def bid_tool(opportunity_id):
    from fastmcp import Client
    from govcon.mcp.server import mcp
    async def call():
        async with Client(mcp) as protocol:
            result = await protocol.call_tool("get_bid_analysis", {"opportunity_id": opportunity_id})
            return result.data
    return asyncio.run(call())


def test_f8_preserves_analysis_without_runs_and_missing_errors(db, client):
    from govcon.models import BidDecision
    opp, _, _, _ = matched_opportunity(db, client)
    no_analysis = bid_tool(opp.id)
    assert not no_analysis["ok"] and no_analysis["error"]["code"] == "validation_error"
    missing = bid_tool(-1)
    assert not missing["ok"] and missing["error"]["code"] == "validation_error"
    bid = BidDecision(opportunity_id=opp.id, recommendation="review", strengths={"items": ["Known strength"]}, rules_result={})
    db.add(bid)
    db.commit()
    result = bid_tool(opp.id)
    assert result["ok"] and result["data"]["opportunity_id"] == opp.id
    assert result["data"]["recent_bundle_runs"] == [] and result["data"]["decision_package"] is None
    assert result["data"]["bid_decision"]["id"] == bid.id
    assert result["data"]["bid_decision"]["strengths"] == {"items": ["Known strength"]}


def test_f8_protocol_reads_real_decision_history(db, client):
    from govcon.config import Settings
    from govcon.decision.engine import run_decision_bundle, list_decision_runs
    from govcon.models import BidDecision, DecisionRun
    opp, _, _, _ = matched_opportunity(db, client)
    db.add(BidDecision(opportunity_id=opp.id, recommendation="review", rules_result={}))
    configured = Settings(_env_file=None, decision_primary_provider="rules", decision_fallback_provider="rules",
                          deepseek_api_key=None, anthropic_api_key=None, openai_api_key=None, jev_api_key=None)
    for bundle in ["opportunity_triage"] * 6 + ["submission_readiness"]:
        run_decision_bundle(db, opportunity_id=opp.id, bundle_name=bundle, settings=configured)
    db.commit()
    expected = list_decision_runs(db, opportunity_id=opp.id, limit=5)
    before = db.scalar(select(func.count()).select_from(DecisionRun).where(DecisionRun.opportunity_id == opp.id))
    for _ in range(2):
        result = bid_tool(opp.id)
        assert result["ok"], result["error"]
        runs = result["data"]["recent_bundle_runs"]
        assert [row["id"] for row in runs] == [row.id for row in expected]
        for actual, original in zip(runs, expected):
            assert set(actual) == {"id", "bundle_name", "provider", "status", "created_at"}
            assert actual["bundle_name"] == original.bundle_name and actual["provider"] == "rules"
            assert actual["status"] == original.result.get("status")
    assert db.scalar(select(func.count()).select_from(DecisionRun).where(DecisionRun.opportunity_id == opp.id)) == before


def stat_value(text, label):
    return int(re.search(r'<div class="label">' + re.escape(label) + r'</div>\s*<div class="value[^\"]*">(\d+)</div>', text).group(1))


def matrix_fixture(db, client):
    from govcon.models import Requirement
    from govcon.compliance.metrics import record_matrix_run
    opp, _, _, _ = matched_opportunity(db, client)
    for status in ("missing", "unknown", "needs_review"):
        db.add(Requirement(opportunity_id=opp.id, requirement_text=f"Synthetic {status} requirement",
                           mandatory=True, severity="high", status=status, requirement_type="technical"))
    db.flush()
    counts, run_id = record_matrix_run(db, opp.id)
    db.commit()
    return opp, counts, run_id


def test_f4_preserves_empty_requirements_and_matrix_counts(db, client):
    empty, _, _, _ = matched_opportunity(db, client)
    assert "No compliance run yet" in client.get(f"/workspace/{empty.id}?tab=compliance").text
    assert "No requirements extracted yet" in client.get(f"/workspace/{empty.id}?tab=requirements").text
    opp, counts, _ = matrix_fixture(db, client)
    page = client.get(f"/workspace/{opp.id}?tab=compliance")
    assert page.status_code == 200
    assert stat_value(page.text, "Mandatory Total") == 3
    for label, expected in [("Satisfied", 0), ("Missing", 1), ("Unknown", 1), ("Needs Review", 1)]:
        assert stat_value(page.text, label) == expected
    assert "Synthetic missing requirement" in page.text
    assert "Synthetic unknown requirement" in client.get(f"/workspace/{opp.id}?tab=requirements").text


def test_f4_matrix_survives_other_runs_and_failed_preflight_is_visible(db, client):
    from govcon.compliance.matrix import record_run
    opp, _, _ = matrix_fixture(db, client)
    record_run(db, opportunity_id=opp.id, run_type="submission_preflight", run_version="test",
               output={"ready": False, "status": "not_ready", "items": [
                   {"check": "required_files", "status": "fail", "reason": "Synthetic pricing.xlsx missing"}]})
    record_run(db, opportunity_id=opp.id, run_type="document_inventory", run_version="test", output={})
    db.commit()
    page = client.get(f"/workspace/{opp.id}?tab=compliance")
    assert stat_value(page.text, "Mandatory Total") == 3, "Inventory/preflight runs must not replace matrix counts"
    assert "Pre-flight: FAIL" in page.text and "Synthetic pricing.xlsx missing" in page.text


def test_f4_current_preflight_pass_and_stale_requirements(db, client, tmp_path):
    from govcon.compliance.matrix import override_requirement
    from govcon.compliance.metrics import record_matrix_run
    from govcon.models import Requirement
    opp, actor, _ = ready_submission(db, client, tmp_path)
    record_matrix_run(db, opp.id)
    db.commit()
    page = client.get(f"/workspace/{opp.id}?tab=compliance")
    assert "Pre-flight: PASS" in page.text, "Current readiness must render from the separate preflight result"
    requirement = db.scalar(select(Requirement).where(Requirement.opportunity_id == opp.id))
    override_requirement(db, requirement_id=requirement.id, status="missing", actor=actor,
                         reason="Synthetic change after preflight", expected_version=requirement.version)
    db.commit()
    page = client.get(f"/workspace/{opp.id}?tab=compliance")
    assert "Pre-flight: STALE" in page.text and "Pre-flight: PASS" not in page.text


def test_f5_preserves_confirmation_and_outcome_forms(db, client, tmp_path):
    opp, _, sub = ready_submission(db, client, tmp_path)
    page = client.get(f"/workspace/{opp.id}?tab=submission")
    form = next(f for f in Forms(page.text).forms if f["attrs"].get("action") ==
                f"/workspace/{opp.id}/submission/approve")
    fields = {field["name"] for field in form["fields"]}
    assert {"csrf_token", "expected_version", "confirmation_number", "submitted_at"} <= fields
    assert 'name="confirmation_notes"' in page.text and "Record Manual Submission" in page.text
    response = client.post(form["attrs"]["action"], data={
        "expected_version": sub.version, "confirmation_number": "SYNTHETIC-CHECKLIST"})
    assert "notice=" in response.headers["location"]
    page = client.get(f"/workspace/{opp.id}?tab=submission")
    assert f'action="/workspace/{opp.id}/record-outcome"' in page.text
    assert f'action="/workspace/{opp.id}/submission/approve"' not in page.text
    for field in ("outcome", "win_reason", "loss_reason", "no_bid_reason", "lessons_learned"):
        assert f'name="{field}"' in page.text


def test_f5_preserves_unapproved_submission_gate(db, client):
    opp, _, _, _ = matched_opportunity(db, client, role="approver", stage="evaluating")
    page = client.get(f"/workspace/{opp.id}?tab=submission")
    assert "Submission package available after proposal is approved" in page.text
    assert f'action="/workspace/{opp.id}/submission/approve"' not in page.text


def test_f5_submission_renders_real_checklist_and_instructions(db, client, tmp_path):
    from markupsafe import escape
    from govcon.submissions.checklist import generate_final_checklist, generate_step_by_step_instructions
    opp, _, sub = ready_submission(db, client, tmp_path)
    page = client.get(f"/workspace/{opp.id}?tab=submission")
    assert "Submission Instructions" in page.text, "Render instructions from the existing instruction service"
    for name in ["proposal.docx", "pricing.xlsx", "release@example.test", "UTC"]:
        assert name in page.text
    checklist = generate_final_checklist(db, opportunity_id=opp.id)
    for item in checklist["items"]:
        assert escape(item["label"]) in page.text
        if item["detail"]:
            assert escape(item["detail"]) in page.text
    for step in generate_step_by_step_instructions(db, opportunity_id=opp.id):
        assert escape(step["title"]) in page.text and escape(step["description"]) in page.text
    assert sub.submission_deadline.isoformat() in page.text


def test_f5_portal_unknown_and_unsafe_instructions_are_visible_and_escaped(db, client, tmp_path):
    from markupsafe import escape
    opp, _, sub = ready_submission(db, client, tmp_path)
    unsafe = '<script>alert("synthetic")</script>'
    sub.submission_method = "portal"
    sub.portal_url = "https://portal.example.test/" + unsafe
    sub.portal_name = "Synthetic portal"
    sub.submission_destination = sub.portal_url
    sub.recipient_email = None
    sub.required_actions = {"special_instructions": [unsafe]}
    db.commit()
    page = client.get(f"/workspace/{opp.id}?tab=submission")
    assert "Upload via portal" in page.text and escape(sub.portal_url) in page.text
    assert unsafe not in page.text and escape(unsafe) in page.text
    sub.submission_method = None
    sub.submission_destination = None
    sub.portal_url = None
    sub.portal_name = None
    db.commit()
    page = client.get(f"/workspace/{opp.id}?tab=submission")
    assert "Submission method not yet determined" in page.text
    assert "No destination determined" in page.text


def compliance_run_command():
    import click
    from typer.main import get_command
    from govcon.cli import compliance_app
    group = get_command(compliance_app)
    return group.get_command(click.Context(group), "run")


def test_f7_preserves_actual_compliance_cli_options():
    command = compliance_run_command()
    with command.make_context("run", ["--opportunity-id", "42", "--no-ai", "--force"]) as context:
        assert context.params["opportunity_id"] == 42
        assert context.params["no_ai"] is True and context.params["force"] is True


def test_f7_empty_compliance_command_is_accepted_by_cli(db, client):
    opp, _, _, _ = matched_opportunity(db, client)
    page = client.get(f"/workspace/{opp.id}?tab=compliance")
    displayed = re.search(r"<code>(govcon compliance run .*?)</code>", page.text).group(1)
    words = shlex.split(displayed)
    assert words[:3] == ["govcon", "compliance", "run"]
    with compliance_run_command().make_context("run", words[3:]) as context:
        assert context.params["opportunity_id"] == opp.id


def test_touched_workspace_tabs_render_real_ready_state_without_mutation(db, client, tmp_path):
    from govcon.models import Proposal
    opp, _, sub = ready_submission(db, client, tmp_path)
    proposal = db.scalar(select(Proposal).where(Proposal.opportunity_id == opp.id))
    review = db.scalar(select(ReviewSession).where(ReviewSession.opportunity_id == opp.id))
    before = (proposal.version, proposal.status, sub.version, sub.status, review.version, review.status,
              db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.opportunity_id == opp.id)))
    for tab in ("overview", "ai_decision", "requirements", "market", "awards", "products", "pricing",
                "competitors", "compliance", "review", "proposal", "submission", "activity"):
        page = client.get(f"/workspace/{opp.id}?tab={tab}")
        assert page.status_code == 200, tab
        assert opp.title in page.text, tab
    db.expire_all()
    after = (proposal.version, proposal.status, sub.version, sub.status, review.version, review.status,
             db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.opportunity_id == opp.id)))
    assert after == before


def test_f1_concurrent_pursue_creates_only_one_workspace(db, client):
    opp, match, _, token = matched_opportunity(db, client)
    match_id = match.id
    barrier = Barrier(2)
    def pursue():
        with CsrfTestClient(create_app(), follow_redirects=False) as worker:
            worker.cookies.set("govcon_session", token)
            barrier.wait(timeout=10)
            return worker.post("/inbox/action", data={"match_id": match_id, "action": "pursuing"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(pursue) for _ in range(2)]
        responses = [future.result(timeout=30) for future in futures]
    assert all(response.status_code == 200 and response.content == b"" for response in responses)
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Pursuit).where(Pursuit.opportunity_id == opp.id)) == 1
    assert db.scalar(select(func.count()).select_from(AuditEvent).where(
        AuditEvent.opportunity_id == opp.id, AuditEvent.action_type == "pursuit_created")) == 1


def test_f2_fresh_start_workspace_and_rejected_assignee(db, client):
    opp, _, actor, _ = matched_opportunity(db, client, role="approver")
    inactive, _ = _make_user(db, f"inactive-{uuid4().hex}@example.test", "reviewer")
    inactive.is_active = False
    db.commit()
    for _ in range(2):
        response = client.post(f"/opp/{opp.id}/start-workspace")
        assert response.status_code == 303 and response.headers["location"] == f"/workspace/{opp.id}"
    page = client.get(f"/workspace/{opp.id}?tab=review")
    assert f'action="/workspace/{opp.id}/assign"' in page.text
    assert f'value="{inactive.id}"' not in page.text
    rejected = client.post(f"/workspace/{opp.id}/assign", data={"user_id": inactive.id})
    assert rejected.status_code == 303 and "error=" in rejected.headers["location"]
    db.expire_all()
    assert db.scalar(select(ReviewSession).where(ReviewSession.opportunity_id == opp.id)) is None
    accepted = client.post(f"/workspace/{opp.id}/assign", data={"user_id": actor.id})
    assert accepted.status_code == 303 and "notice=" in accepted.headers["location"]
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Pursuit).where(Pursuit.opportunity_id == opp.id)) == 1
