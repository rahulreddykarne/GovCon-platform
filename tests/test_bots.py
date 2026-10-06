"""Orchestrator handoffs, idempotency, failure, and the approvals queue."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.bots.orchestrator import execute_orchestrator
from govcon.bots.store import decide_approval
from govcon.bots.workflows import execute_discovery
from govcon.config import get_settings
from govcon.decision.provider import DecisionProviderUnavailable
from govcon.models import BotApproval, BotRun, Opportunity, OpportunityEvent, Pursuit, Watchlist


@pytest.fixture()
def session(upgraded_engine) -> Session:
    with Session(upgraded_engine) as db:
        yield db
        db.rollback()


def _settings(tmp_path):
    base = get_settings()
    return base.model_copy(update={
        "outbox_dir": tmp_path,
        "jev_api_key": None,
        "smtp_host": None,
        "alert_email_to": None,
        "decision_primary_provider": "rules",
        "decision_fallback_provider": "rules",
        "deepseek_api_key": None,
        "anthropic_api_key": None,
        "openai_api_key": None,
    })


def _notice(session: Session, *, psc: str = "ZZ99") -> Opportunity:
    opportunity = Opportunity(
        source="demo",
        source_id=f"DEMO-{uuid4().hex[:8]}",
        title="Sanitized demo notice",
        description="DEMO ONLY. The contractor shall package the demo item.",
        psc_code=psc,
        status="open",
        response_deadline=datetime.now(UTC) + timedelta(days=14),
        raw={"demo": True},
        raw_hash=uuid4().hex,
        agency_path="Demo agency (not a real office)",
    )
    session.add(opportunity)
    session.flush()
    session.add(OpportunityEvent(
        opportunity_id=opportunity.id, event_type="created", detected_at=datetime.now(UTC),
    ))
    session.flush()
    return opportunity


def _watch(session: Session, psc: str) -> None:
    session.add(Watchlist(name=f"demo-{psc}-{uuid4().hex[:6]}", enabled=True, psc_codes=[psc]))
    session.flush()


def _block_jev(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_network(*_args, **_kwargs):
        raise DecisionProviderUnavailable("blocked in test")

    monkeypatch.setattr("govcon.decision.engine.JevDecisionProvider.from_settings", _no_network)


def _pull_factory():
    state = {"n": 0, "id": None}

    def pull(session: Session, settings) -> tuple[datetime, dict]:
        del settings
        state["n"] += 1
        started = datetime.now(UTC)
        opportunity = _notice(session)
        state["id"] = opportunity.id
        return started, {
            "sam": {"status": "succeeded", "error": None, "fetched": 1, "inserted": 1, "updated": 0},
            "dibbs": {"status": "succeeded", "error": None, "fetched": 0, "inserted": 0, "updated": 0},
        }

    pull.state = state
    return pull


def test_orchestrator_handoff_opens_an_approval_and_does_not_submit(
    session: Session, tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_jev(monkeypatch)
    _watch(session, "ZZ99")
    pull = _pull_factory()
    monkeypatch.setattr("govcon.bots.workflows.pull_sources", pull)
    monkeypatch.setattr("govcon.alerts.digest.smtplib.SMTP", lambda *a, **k: (_ for _ in ()).throw(AssertionError("smtp")))
    run = execute_orchestrator(session, _settings(tmp_path), trigger="test", slot="handoff-1", pull=True)
    opportunity_id = pull.state["id"]
    assert opportunity_id is not None
    session.flush()
    assert run.outputs["opportunity_state"][str(opportunity_id)] == "needs_decision"
    assert run.outputs["any_incomplete"] is False
    approval = session.scalar(select(BotApproval).where(BotApproval.opportunity_id == opportunity_id, BotApproval.kind == "bid_recommendation"))
    assert approval is not None and approval.status == "pending"
    assert session.scalar(select(Pursuit).where(Pursuit.opportunity_id == opportunity_id)) is None
    providers = session.scalar(
        select(BotRun).where(BotRun.bot_name == "bid_decision", BotRun.opportunity_id == opportunity_id)
    )
    assert providers is not None
    assert "jev" not in (providers.outputs or {}).get("providers", [])


def test_discovery_idempotency_key_does_not_pull_twice(session: Session, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    pull = _pull_factory()
    monkeypatch.setattr("govcon.bots.workflows.pull_sources", pull)
    first = execute_discovery(session, _settings(tmp_path), slot="dedup-1", trigger="test", parent_run_id=None, pull=True)
    second = execute_discovery(session, _settings(tmp_path), slot="dedup-1", trigger="test", parent_run_id=None, pull=True)
    assert first.id == second.id
    assert pull.state["n"] == 1


def test_failed_document_bot_leaves_the_opportunity_incomplete(
    session: Session, tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_jev(monkeypatch)
    _watch(session, "ZZ99")
    pull = _pull_factory()
    monkeypatch.setattr("govcon.bots.workflows.pull_sources", pull)

    def failed_document(session, settings, *, opportunity_id, trigger, parent_run_id):
        from govcon.bots.store import begin_run, finish_run

        del settings
        run, started = begin_run(
            session, bot_name="document", idempotency_key=f"document:{opportunity_id}:forced",
            trigger=trigger, opportunity_id=opportunity_id, parent_run_id=parent_run_id,
            inputs={"forced": True},
        )
        if started:
            finish_run(run, "failed", error="could not read the notice", outputs={
                "state": "incomplete", "analysis_complete": False,
            })
        return run

    monkeypatch.setattr("govcon.bots.orchestrator.execute_document", failed_document)
    run = execute_orchestrator(session, _settings(tmp_path), trigger="test", slot="fail-doc", pull=True)
    opportunity_id = pull.state["id"]
    assert run.outputs["opportunity_state"][str(opportunity_id)] == "incomplete"
    assert run.outputs["any_incomplete"] is True
    assert run.outputs["state"] == "incomplete"
    bid = session.scalar(select(BotRun).where(BotRun.bot_name == "bid_decision", BotRun.opportunity_id == opportunity_id))
    assert bid is None


def test_approval_records_the_human_and_does_not_change_sharing(
    session: Session, tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from govcon.collaboration.users import hash_password
    from govcon.models import User

    _block_jev(monkeypatch)
    _watch(session, "ZZ99")
    pull = _pull_factory()
    monkeypatch.setattr("govcon.bots.workflows.pull_sources", pull)
    person = User(
        email=f"approver-{uuid4().hex[:8]}@example.test",
        display_name="Approver",
        password_hash=hash_password("TestPassword123!"),
        role="approver",
        is_active=True,
    )
    session.add(person)
    session.flush()
    execute_orchestrator(session, _settings(tmp_path), trigger="test", slot="approve-1", pull=True)
    approval = session.scalar(select(BotApproval).where(BotApproval.kind == "bid_recommendation"))
    assert approval is not None
    before = (
        get_settings().ai_external_allowed_for_proprietary,
        get_settings().ai_external_allowed_for_fci,
        get_settings().ai_external_allowed_for_cui,
    )
    decided = decide_approval(session, approval.id, user_id=person.id, status="approved", note="look only")
    assert decided is not None and decided.status == "approved"
    after = (
        get_settings().ai_external_allowed_for_proprietary,
        get_settings().ai_external_allowed_for_fci,
        get_settings().ai_external_allowed_for_cui,
    )
    assert before == after


def test_bots_page_requires_login_and_lists_the_catalog(upgraded_engine) -> None:
    from govcon.collaboration.users import create_session, hash_password
    from govcon.models import User
    from govcon.web.app import create_app
    from web_client import CsrfTestClient

    with Session(upgraded_engine) as db:
        user = User(
            email=f"bots-{uuid4().hex[:8]}@example.test",
            display_name="Bot Tester",
            password_hash=hash_password("TestPassword123!"),
            role="owner",
            is_active=True,
        )
        db.add(user)
        db.flush()
        token = create_session(db, user)
        db.commit()
    with CsrfTestClient(create_app(), follow_redirects=False) as client:
        anonymous = client.get("/bots")
        assert anonymous.status_code == 303
        page = client.get("/bots", cookies={"govcon_session": token})
    assert page.status_code == 200
    assert b"Orchestrator" in page.content
    assert b"Approvals" in page.content
    assert b"does not send email" in page.content.lower() or b"Nothing here sends email" in page.content
