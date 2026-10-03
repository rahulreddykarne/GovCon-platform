"""Regression coverage for the attached production findings F11--F20."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import re
from unittest.mock import Mock
from urllib.parse import urlencode
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from govcon.ai.structured import StructuredCallError, resolve_prompt
from govcon.config import Settings
from govcon.models import (
    AIAnalysis, AuditEvent, Match, Opportunity, OpportunityEvent, PromptRegistryEntry,
    Proposal, ProposalVersion, Pursuit, ReviewSession, StoredFile, Submission, User, Watchlist,
)
from govcon.web.app import create_app


@pytest.fixture
def db(upgraded_engine):
    with Session(upgraded_engine) as session:
        yield session
        session.rollback()


def opportunity(db, **fields):
    defaults = dict(source="sam", source_id=f"f11-20-{uuid4().hex}", title="Regression opportunity",
                    status="open", raw={}, response_deadline=datetime.now(UTC) + timedelta(days=10))
    defaults.update(fields)
    row = Opportunity(**defaults)
    db.add(row)
    db.flush()
    return row


def user(db, role="approver"):
    row = User(email=f"regression-{uuid4().hex}@example.test", display_name="Regression actor",
               password_hash="unused", role=role, is_active=True)
    db.add(row)
    db.flush()
    return row


def web_session(monkeypatch, db, actor):
    from govcon.web import routes

    @contextmanager
    def scope(*args, **kwargs):
        yield db

    monkeypatch.setattr(routes, "session_scope", scope)
    monkeypatch.setattr(routes, "_require_login", lambda request: actor)
    monkeypatch.setattr(routes, "_current_user", lambda request: None)
    monkeypatch.setattr(routes, "_unread_count", lambda actor: 0)


def token(client):
    response = client.get("/login")
    assert response.status_code == 200
    return re.search(r'name="csrf_token" value="([a-f0-9]+)"', response.text).group(1)


@pytest.mark.parametrize("status,days,expected", [
    ("open", -30, "ineligible"), ("open", None, "unknown"), ("open", 5, "eligible"),
    ("active", 5, "ineligible"), ("closed", 5, "ineligible"),
])
def test_f11_shared_deadline_eligibility(status, days, expected):
    from govcon.matching.engine import _is_open
    from govcon.matching.semantic import _compact_opp, is_eligible_for_pursuit
    now = datetime.now(UTC)
    deadline = None if days is None else (now + timedelta(days=days)).replace(tzinfo=None)
    opp = Opportunity(source="sam", source_id="f11", status=status, response_deadline=deadline)
    result = _compact_opp(opp)
    assert result["eligibility_status"] == expected
    assert result["eligible_for_pursuit"] == is_eligible_for_pursuit(opp) == (expected == "eligible")
    assert _is_open(opp, now) == (expected != "ineligible")
    if expected != "eligible":
        assert "deadline" in result["ineligible_reason"] or "status" in result["ineligible_reason"]


@pytest.mark.parametrize("stage", ["drafting", "review", "ready_to_submit", "submitted", "won", "lost", "cancelled"])
def test_f12_pipeline_renders_actual_progress(db, monkeypatch, stage):
    opp = opportunity(db, title=f"Pipeline stage {stage}")
    db.add_all([Pursuit(opportunity_id=opp.id, stage=stage),
                ReviewSession(opportunity_id=opp.id, status="approved_to_bid", final_approval_status="approved_to_bid")])
    db.flush()
    web_session(monkeypatch, db, user(db))
    with TestClient(create_app(), follow_redirects=False) as client:
        response = client.get("/pipeline")
    assert response.status_code == 200
    columns = {c["key"]: c["cards"] for c in response.context["columns"]}
    assert any(card["opp_id"] == opp.id for card in columns[stage])
    assert all(card["opp_id"] != opp.id for card in columns["bid_approved"])


@pytest.mark.parametrize("fields,valid", [
    ([("award_amount", "95000")], True),
    ([("award_amount", "95000"), ("award_amount", "")], False),
    ([("award_amount", "oops")], False), ([("award_amount", "nan")], False),
    ([("award_amount", "inf")], False), ([("award_amount", "-1")], False),
    ([("known_winning_price", "1"), ("known_winning_price", "2")], False),
    ([("win_margin_pct", "nan")], False),
])
def test_f13_actual_outcome_route_rejects_bad_money(db, monkeypatch, fields, valid):
    from govcon.web import routes
    web_session(monkeypatch, db, user(db))
    record = Mock()
    monkeypatch.setattr(routes, "record_outcome", record)
    with TestClient(create_app(), follow_redirects=False) as client:
        csrf = token(client)
        response = client.post("/workspace/1/record-outcome", content=urlencode([
            ("csrf_token", csrf), ("outcome", "won"), *fields,
        ]), headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert response.status_code == 303
    if valid:
        assert record.call_args.kwargs["award_amount"] == 95000
        assert "notice=" in response.headers["location"]
    else:
        record.assert_not_called()
        assert "error=" in response.headers["location"]


def test_f14_registry_denial_never_falls_back(db):
    from govcon.prompting.registry import sync_prompts
    settings = Settings(_env_file=None, prompt_allow_disk_fallback=True)
    sync_prompts(db, settings.resolved_prompt_root())
    db.execute(update(PromptRegistryEntry).where(
        PromptRegistryEntry.prompt_name == "solicitation_analysis"
    ).values(active=False))
    with pytest.raises(StructuredCallError, match="registry_error"):
        resolve_prompt(db, "solicitation_analysis", settings)


def test_f14_absence_requires_explicit_bootstrap(db):
    from govcon.prompting.registry import PromptRegistryAbsent
    from govcon.ai import structured
    with pytest.MonkeyPatch.context() as mp:
        def absent(*args, **kwargs):
            raise PromptRegistryAbsent("registry is not synchronized")
        mp.setattr(structured, "load_prompt", absent)
        with pytest.raises(StructuredCallError, match="registry_absent"):
            resolve_prompt(db, "solicitation_analysis", Settings(_env_file=None))
        assert resolve_prompt(db, "solicitation_analysis", Settings(_env_file=None, prompt_allow_disk_fallback=True)).name == "solicitation_analysis"
    with pytest.raises(StructuredCallError, match="registry_unavailable"):
        resolve_prompt(None, "solicitation_analysis", Settings(_env_file=None))


@pytest.mark.parametrize("failure", [ValueError("no active version"), FileNotFoundError("registry path missing"), RuntimeError("database unavailable")])
def test_f14_registry_errors_fail_closed_even_in_bootstrap(monkeypatch, failure):
    from govcon.ai import structured
    monkeypatch.setattr(structured, "load_prompt", Mock(side_effect=failure))
    disk = Mock()
    monkeypatch.setattr(structured, "load_prompt_from_disk", disk)
    with pytest.raises(StructuredCallError, match="registry_error"):
        resolve_prompt(Mock(), "solicitation_analysis", Settings(_env_file=None, prompt_allow_disk_fallback=True))
    disk.assert_not_called()


def test_f14_solicitation_analysis_obeys_registry_denial(db, monkeypatch):
    from govcon.enrich.summarize import run_solicitation_analysis
    from govcon.prompting.registry import sync_prompts
    settings = Settings(_env_file=None, prompt_allow_disk_fallback=True)
    sync_prompts(db, settings.resolved_prompt_root())
    db.execute(update(PromptRegistryEntry).where(
        PromptRegistryEntry.prompt_name == "solicitation_analysis"
    ).values(active=False))
    opp = opportunity(db)
    db.add(StoredFile(classification="PUBLIC", source_origin="synthetic_test_fixture", opportunity_id=opp.id, filename="source.txt", extracted_text="source content", extraction_status="success"))
    db.flush()
    provider = Mock()
    monkeypatch.setattr("govcon.ai.structured.get_provider", provider)
    assert run_solicitation_analysis(db, opp, settings=settings, force=True) is None
    provider.assert_not_called()


@pytest.mark.parametrize("primary", ["deepseek", "anthropic", "openai"])
def test_f15_each_primary_drafts_with_ai_after_web_approval(db, monkeypatch, primary):
    """Approval queues generation (ADR-062); the task drafts with AI for any configured primary."""
    from types import SimpleNamespace
    from govcon.tasks.handlers import proposal as handler
    from govcon.tasks.registry import StepContext
    from govcon.web import routes
    settings = Settings(_env_file=None, ai_primary_provider=primary, **{f"{primary}_api_key": "test-key"})
    # Clear keys inherited from the environment so the test only configures the primary.
    for name in {"deepseek", "anthropic", "openai"} - {primary}:
        setattr(settings, f"{name}_api_key", None)
    monkeypatch.setattr("govcon.config.get_settings", lambda: settings)
    web_session(monkeypatch, db, user(db))
    monkeypatch.setattr(routes, "finalize_approval", Mock())
    with TestClient(create_app(settings), follow_redirects=False) as client:
        response = client.post("/workspace/1/approve", data={
            "csrf_token": token(client), "decision": "approve_to_bid", "expected_version": "1",
        })
    assert response.status_code == 303 and "notice=" in response.headers["location"]
    prepared = Mock()
    prepare = Mock(return_value=prepared)
    monkeypatch.setattr(handler, "_current_inputs", lambda *args: {"without_ai": False})
    monkeypatch.setattr("govcon.proposals.drafting.prepare_draft_call", prepare)
    ctx = StepContext(settings=settings, task_id=1, opportunity_id=1, payload={}, input_revision={})
    draft = handler._prepare(db, SimpleNamespace(opportunity_id=1), ctx)
    assert draft.prepared is prepared, f"{primary} must take the AI drafting path, not the placeholder"


@pytest.mark.parametrize("primary", ["deepseek", "anthropic", "openai"])
def test_f15_source_change_defaults_to_configured_primary(db, monkeypatch, primary):
    from govcon.workflow import invalidation
    from govcon.workflow.source_events import MATERIAL_SOURCE_CHANGE_EVENT
    settings = Settings(_env_file=None, ai_primary_provider=primary, **{f"{primary}_api_key": "test-key"})
    opp = opportunity(db)
    db.add(Pursuit(opportunity_id=opp.id, stage="sourcing"))
    db.add(OpportunityEvent(opportunity_id=opp.id, event_type=MATERIAL_SOURCE_CHANGE_EVENT,
                            new_value={"value": {"level": "material"}}))
    db.flush()
    revalidate = Mock(return_value={})
    monkeypatch.setattr(invalidation, "_revalidate_sources", revalidate)
    result = invalidation.process_pending_source_changes(db, settings=settings, fetch_attachments=False, opportunity_ids={opp.id})
    assert result["errors"] == []
    assert revalidate.call_args.kwargs["use_ai"] is True


def test_f15_proposal_versions_record_actual_provider_and_model(db, monkeypatch):
    from govcon.ai.structured import StructuredCallResult
    from govcon.compliance.schemas import ProposalDraftV1
    from govcon.proposals.service import generate_proposal
    from govcon.prompting.registry import load_prompt_from_disk
    opp = opportunity(db)
    db.add_all([Pursuit(opportunity_id=opp.id, stage="bid_approved"),
                ReviewSession(opportunity_id=opp.id, status="approved_to_bid", final_approval_status="approved_to_bid")])
    db.flush()
    result = StructuredCallResult(output=ProposalDraftV1(), analysis=AIAnalysis(provider="anthropic", model="actual-model"),
                                  prompt=load_prompt_from_disk(Settings(_env_file=None).resolved_prompt_root(), "proposal_drafting"))
    monkeypatch.setattr("govcon.proposals.drafting.run_structured_prompt", Mock(return_value=result))
    generate_proposal(db, opportunity_id=opp.id, actor=user(db))
    proposal = db.scalar(select(Proposal).where(Proposal.opportunity_id == opp.id))
    version = db.get(ProposalVersion, proposal.current_version_id)
    assert (version.provider, version.model) == ("anthropic", "actual-model")


@pytest.mark.parametrize("time_text", ["25:99", "24:00", "13:00 pm", "00:00 am", "12:60", "9:99am", "12:00xm", "9999hours", "1:23amjunk"])
def test_f16_malformed_times_return_unknown_with_source(time_text):
    from govcon.compliance.deterministic import deadline_timezone_consistent, parse_source_deadline
    values = {"response_deadline_date": "2027-01-15", "response_deadline_time": time_text, "deadline_timezone": "ET"}
    assert parse_source_deadline("2027-01-15", time_text, "ET") is None
    result = deadline_timezone_consistent(values, datetime(2027, 1, 15))
    assert result.status == "unknown" and result.evidence["source"] == values


def test_f16_boundaries_and_naive_datetimes():
    from govcon.compliance.deterministic import (
        deadline_not_passed, deadline_timezone_consistent, parse_source_deadline,
        submission_before_deadline, SubmissionPackage,
    )
    assert parse_source_deadline("2027-01-15", "12am", "UTC").hour == 0
    assert parse_source_deadline("2027-01-15", "12pm", "UTC").hour == 12
    assert parse_source_deadline("2027-01-15", "23:59", "UTC").hour == 23
    assert parse_source_deadline(datetime(2027, 1, 15), "12pm", "UTC") is None
    values = {"response_deadline_date": "2027-01-15", "response_deadline_time": "12pm", "deadline_timezone": "UTC"}
    assert deadline_timezone_consistent(values, datetime(2027, 1, 15, 12)).status == "pass"
    assert deadline_not_passed(datetime(2027, 1, 15), datetime(2027, 1, 14, tzinfo=UTC)).status == "pass"
    assert submission_before_deadline(SubmissionPackage(planned_submission_at=datetime(2027, 1, 14)), datetime(2027, 1, 15, tzinfo=UTC)).status == "pass"


@pytest.mark.parametrize("stage", ["submitted", "won", "lost", "cancelled", "no_bid"])
@pytest.mark.parametrize("field,value", [("quote_price", 9000), ("sourcing_cost", 5000), ("supplier", "new supplier"), ("notes", "new notes")])
def test_f17_reviewers_cannot_edit_locked_commercial_facts(db, monkeypatch, stage, field, value):
    from govcon.mcp.operations import op_update_pursuit
    opp = opportunity(db)
    pursuit = Pursuit(opportunity_id=opp.id, stage=stage, quote_price=Decimal("10000"), notes="original")
    db.add(pursuit)
    db.flush()
    monkeypatch.setattr("govcon.mcp.operations.current_actor", lambda *args: user(db, "reviewer"))
    with pytest.raises(ValueError, match="locked"):
        op_update_pursuit(db, opp.id, expected_version=pursuit.version, **{field: value})
    assert pursuit.quote_price == Decimal("10000") and pursuit.notes == "original"


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), float("-inf")])
def test_f17_financial_validation_at_service_boundary(db, monkeypatch, value):
    from govcon.mcp.operations import op_update_pursuit
    opp = opportunity(db)
    pursuit = Pursuit(opportunity_id=opp.id, stage="sourcing")
    db.add(pursuit)
    db.flush()
    actor = user(db, "reviewer")
    monkeypatch.setattr("govcon.mcp.operations.current_actor", lambda *args: actor)
    with pytest.raises(ValueError, match="finite"):
        op_update_pursuit(db, opp.id, expected_version=pursuit.version, quote_price=value)
    assert pursuit.quote_price is None


@pytest.mark.parametrize("stage,proposal_status", [("bid_approved", "draft"), ("drafting", "ai_generated"), ("review", "red_teamed"), ("ready_to_submit", "final_approved")])
def test_f17_pre_submission_edits_reopen_approvals_and_drafts(db, monkeypatch, stage, proposal_status):
    from govcon.mcp.operations import op_update_pursuit
    opp = opportunity(db)
    actor = user(db, "reviewer")
    pursuit = Pursuit(opportunity_id=opp.id, stage=stage, quote_price=Decimal("100"))
    review = ReviewSession(opportunity_id=opp.id, status="approved_to_bid", final_approval_status="approved_to_bid")
    db.add_all([pursuit, review])
    db.flush()
    proposal = Proposal(opportunity_id=opp.id, pursuit_id=pursuit.id, status=proposal_status)
    submission = Submission(opportunity_id=opp.id, pursuit_id=pursuit.id, status="ready", readiness_status="ready")
    db.add_all([proposal, submission])
    db.flush()
    monkeypatch.setattr("govcon.mcp.operations.current_actor", lambda *args: actor)
    op_update_pursuit(db, opp.id, expected_version=pursuit.version, quote_price=200)
    assert pursuit.stage == "evaluating" and pursuit.quote_price == Decimal("200")
    assert review.final_approval_status is None and review.status == "ready_for_review"
    assert proposal.status == "returned_for_fix" and proposal.approved_version_id is None
    assert submission.status == "preparing" and submission.readiness_status == "not_ready"


def test_f17_submitted_corrections_are_authorized_and_append_only(db):
    from govcon.collaboration.users import PermissionDenied
    from govcon.workflow.commercial import record_commercial_correction
    opp = opportunity(db)
    pursuit = Pursuit(opportunity_id=opp.id, stage="submitted", quote_price=Decimal("95000"))
    db.add(pursuit)
    db.flush()
    submission = Submission(opportunity_id=opp.id, pursuit_id=pursuit.id, status="submitted", submitted_at=datetime.now(UTC), package_manifest_hash="original-hash")
    db.add(submission)
    db.flush()
    params = dict(expected_version=pursuit.version, changes={"quote_price": 96000}, reason="Correcting a transcription error")
    with pytest.raises(PermissionDenied):
        record_commercial_correction(db, opp.id, actor=user(db, "reviewer"), **params)
    result = record_commercial_correction(db, opp.id, actor=user(db), **params)
    event = db.get(AuditEvent, result["audit_event_id"])
    assert pursuit.quote_price == Decimal("95000") and pursuit.stage == "submitted"
    assert submission.package_manifest_hash == "original-hash"
    assert event.old_value["submitted_facts"]["quote_price"] == "95000"
    assert event.new_value["correction"]["quote_price"] == "96000"
    assert event.new_value["submission_id"] == submission.id


@pytest.mark.parametrize("origin", ["https://attacker.example", "http://testserver:8080", "http://testserver:0", "https://testserver", "null", "http://testserver.evil.test"])
def test_f18_foreign_login_origins_rejected_before_auth(monkeypatch, origin):
    monkeypatch.setattr("govcon.web.routes._current_user", lambda request: None)
    authenticate = Mock()
    monkeypatch.setattr("govcon.web.routes.authenticate", authenticate)
    with TestClient(create_app(Settings(_env_file=None)), follow_redirects=False) as client:
        response = client.post("/login", data={"csrf_token": token(client), "email": "a@b.test", "password": "password"}, headers={"Origin": origin})
    assert response.status_code == 403
    authenticate.assert_not_called()
    assert "govcon_session=" not in response.headers.get("set-cookie", "")


def test_f18_tokens_required_and_bound_to_session(monkeypatch):
    monkeypatch.setattr("govcon.web.routes._current_user", lambda request: None)
    with TestClient(create_app(Settings(_env_file=None)), follow_redirects=False) as client:
        csrf = token(client)
        assert client.post("/logout", data={}, headers={"Origin": "http://testserver"}).status_code == 403
        assert client.post("/logout", data={"csrf_token": "0" * 64}).status_code == 403
        assert client.post("/logout", data={"csrf_token": "é" * 64}).status_code == 403
        assert client.post("/logout", data={"csrf_token": csrf}, headers={"Referer": "http://testserver:8000/page"}).status_code == 403
        client.cookies.set("govcon_session", "different-session")
        assert client.post("/logout", data={"csrf_token": csrf}).status_code == 403
        client.cookies.delete("govcon_session")
        assert client.post("/logout", data={"csrf_token": csrf}, headers={"Referer": "http://testserver/page"}).status_code == 303


def test_f18_https_cookies_and_ttls(db, monkeypatch):
    from govcon.web import routes
    web_session(monkeypatch, db, user(db))
    monkeypatch.setattr(routes, "authenticate", lambda *args: user(db))
    settings = Settings(_env_file=None, session_ttl_hours=2)
    with TestClient(create_app(settings), base_url="https://testserver", follow_redirects=False) as client:
        csrf = token(client)
        response = client.post("/login", data={"csrf_token": csrf, "email": "a@b.test", "password": "password"}, headers={"Origin": "https://testserver:443"})
    cookie = response.headers["set-cookie"]
    assert response.status_code == 303 and "Secure" in cookie and "Max-Age=7200" in cookie
    from govcon.models import UserSession
    row = db.scalars(select(UserSession).order_by(UserSession.id.desc())).first()
    assert timedelta(hours=1, minutes=59) < row.expires_at - datetime.now(UTC) <= timedelta(hours=2)


def test_f18_login_throttle_is_bounded(monkeypatch):
    from govcon.web.security import LoginThrottle
    monkeypatch.setattr("govcon.web.routes._current_user", lambda request: None)
    @contextmanager
    def scope():
        yield Mock()
    monkeypatch.setattr("govcon.web.routes.session_scope", scope)
    monkeypatch.setattr("govcon.web.routes.authenticate", Mock(return_value=None))
    settings = Settings(_env_file=None, login_attempt_limit=2)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        csrf = token(client)
        form = {"csrf_token": csrf, "email": "a@b.test", "password": "password"}
        assert [client.post("/login", data=form).status_code for _ in range(3)] == [401, 401, 429]
    limiter = LoginThrottle(capacity=2)
    assert limiter.allow("one", limit=2, window=300)
    assert limiter.allow("two", limit=2, window=300)
    assert not limiter.allow("three", limit=2, window=300)
    assert len(limiter.entries) == 2


def test_f20_excludes_live_rule_matches_before_vector_limit(db):
    from govcon.matching.semantic import semantic_recommendations_for_watchlist
    vector = [1.0] + [0.0] * 383
    watchlist = Watchlist(name=f"F20 {uuid4().hex}", embedding=vector)
    db.add(watchlist)
    db.flush()
    for i in range(60):
        opp = opportunity(db, embedding=vector)
        db.add(Match(watchlist_id=watchlist.id, opportunity_id=opp.id, score=1, matched_on={}, active=True,
                     status=["new", "seen", "reviewing", "pursuing"][i % 4]))
    inactive = opportunity(db, embedding=vector)
    dismissed = opportunity(db, embedding=vector)
    candidate = opportunity(db, embedding=vector)
    db.add_all([
        Match(watchlist_id=watchlist.id, opportunity_id=inactive.id, score=1, matched_on={}, active=False, status="seen"),
        Match(watchlist_id=watchlist.id, opportunity_id=dismissed.id, score=1, matched_on={}, active=True, status="dismissed"),
    ])
    db.flush()
    # Isolate distance ranking from opportunities created by other test modules.
    db.execute(update(Opportunity).where(Opportunity.id.not_in([inactive.id, dismissed.id, candidate.id])).values(embedding=None))
    # Restore the 60 nearest excluded neighbors to exercise SQL exclusion before LIMIT.
    ids = db.scalars(select(Match.opportunity_id).where(
        Match.watchlist_id == watchlist.id, Match.active.is_(True), Match.status != "dismissed"
    )).all()
    db.execute(update(Opportunity).where(Opportunity.id.in_(ids)).values(embedding=vector))
    result = semantic_recommendations_for_watchlist(db, watchlist.id, limit=10)
    assert {row["id"] for row in result["matches"]} == {inactive.id, dismissed.id, candidate.id}
    assert len(semantic_recommendations_for_watchlist(db, watchlist.id, limit=1)["matches"]) == 1
