"""Readiness fixes, round 3: budget waits, recorded preparation passes, award-match volume,
company facts in drafting, event-loop blocking routes, and pursuit commercial facts on the web."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from test_outcome_suggestions import (  # noqa: F401  (fixtures)
    OUR_UEI,
    SUBMITTED,
    award_notice,
    our_uei,
    run,
    submitted_bid,
    suggestions,
    unique_nsn,
)
from test_web_ui import _make_user
from web_client import CsrfTestClient

from govcon.config import Settings
from govcon.db import session_scope
from govcon.models import (
    AuditEvent,
    Award,
    CompanyRegistration,
    Notification,
    Opportunity,
    Pursuit,
    Task,
)
from govcon.web.app import create_app


@pytest.fixture()
def db(upgraded_engine):
    with Session(upgraded_engine) as session:
        yield session


@pytest.fixture()
def client(upgraded_engine):
    with CsrfTestClient(create_app(), follow_redirects=False) as client:
        yield client


def new_opportunity(db, **values) -> Opportunity:
    opp = Opportunity(source="sam", source_id=uuid4().hex, title=f"R3 {uuid4().hex[:6]}", status="open",
                      response_deadline=datetime.now(UTC) + timedelta(days=30), raw={}, links={}, **values)
    db.add(opp)
    db.commit()
    return opp


# ── 1. budget: lifetime caps, one automatic retry, a share kept for proposals ──

def test_preparation_cannot_spend_the_proposal_share(db):
    from govcon.ai.budget import AIBudgetExceeded, input_bound, reserve

    opp_id = new_opportunity(db).id
    bound = input_bound("", "source")
    configured = Settings(_env_file=None, ai_max_input_tokens_per_opportunity=4 * bound, ai_proposal_budget_share=0.5)

    def take(purpose: str) -> bool:
        try:
            reserve(db, opportunity_id=opp_id, settings=configured, system_prompt="", user_prompt="source",
                    purpose=purpose, provider="fake")
            return True
        except AIBudgetExceeded as exc:
            assert ("kept for proposal drafting" in str(exc)) == (purpose != "proposal_drafting")
            return False

    assert [take("solicitation_analysis") for _ in range(3)] == [True, True, False], "half is kept back"
    assert [take("proposal_drafting") for _ in range(3)] == [True, True, False], "drafting may use the rest"


def _blocked_task(db) -> Task:
    from govcon.tasks import queue

    # Claimed by task id only; no handler runs, so any registered type will do.
    task, _ = queue.enqueue(db, task_type="ai_analysis", opportunity_id=None,
                            input_revision={"probe": uuid4().hex})
    db.commit()
    return task


def _claim_and_block(task_id: int, settings: Settings) -> bool:
    from govcon.tasks import queue

    with session_scope() as s:
        claim = queue.claim(s, worker_id="r3-worker", lease_seconds=60, task_id=task_id, settings=settings)
    if claim is None:
        return False
    with session_scope() as s:
        task = queue.guard_publish(s, claim)
        queue.block(s, task, status="waiting_for_budget", reason="Opportunity input budget exhausted.",
                    owner_role="owner", next_action="raise the cap", settings=settings)
        task.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)  # due again at once
    return True


def test_budget_block_never_auto_starts(db):
    from govcon.tasks import queue
    from govcon.tasks.queue import budget_parked

    task_id = _blocked_task(db).id
    limits = Settings(_env_file=None)
    assert _claim_and_block(task_id, limits), "first claim"
    db.expire_all()
    assert budget_parked(db.get(Task, task_id))
    assert not _claim_and_block(task_id, limits), "waiting_for_budget is never claimed"
    raised = Settings(_env_file=None, ai_max_input_tokens_per_opportunity=limits.ai_max_input_tokens_per_opportunity * 2)
    assert not _claim_and_block(task_id, raised), "a raised env cap still does not auto-start"
    with session_scope() as s:
        task = s.get(Task, task_id)
        queue.requeue(s, task, actor_user_id=None, reason="human resume")
    assert _claim_and_block(task_id, limits), "explicit requeue is the only resume"
    audits = db.scalar(select(func.count()).select_from(AuditEvent).where(
        AuditEvent.entity_type == "tasks", AuditEvent.entity_id == task_id, AuditEvent.action_type == "task_blocked"))
    assert audits == 2, "the second block after a human resume is a new event"


def test_budget_next_action_says_the_cap_does_not_refill():
    from govcon.ai.budget import AIBudgetExceeded
    from govcon.tasks.errors import classify

    outcome = classify(AIBudgetExceeded("exhausted"))
    assert outcome.status == "waiting_for_budget" and "does not refill" in outcome.next_action
    assert "never starts" in outcome.next_action


# ── 2. recorded passes: no transaction or lock during an AI call ─────────────

@pytest.mark.usefixtures("allow_proprietary_ai")
def test_recorded_compliance_run_holds_no_lock_during_ai_calls_and_repeats_none(upgraded_engine, monkeypatch):
    from test_compliance import NOW, _dla_provider, _dla_setup

    from govcon.ai.replay import active_recorder, run_recorded, stable_ids
    from govcon.compliance.pipeline import run_compliance_pipeline
    from govcon.config import get_settings
    from govcon.prompting.registry import sync_prompts
    from govcon.workflow.invalidation import lock_opportunity

    facts = {"sam_registration_status": "active", "sam_expiration_date": "2027-06-30"}
    with Session(upgraded_engine) as s:
        sync_prompts(s, get_settings().resolved_prompt_root())
        live_opp, live_ids = _dla_setup(s)
        recorded_opp, recorded_ids = _dla_setup(s)
        s.commit()
        live_id, recorded_id = live_opp.id, recorded_opp.id

    live_provider = _dla_provider(live_ids)
    monkeypatch.setattr("govcon.ai.structured.get_provider", lambda *a, **k: live_provider)
    with session_scope() as s:
        live = run_compliance_pipeline(s, live_id, now=NOW, company_facts=facts)

    provider = _dla_provider(recorded_ids)
    unlocked: list[bool] = []
    real_complete = provider.complete

    def complete(**kwargs):
        # Another connection must be able to take the workflow lock right now.
        with upgraded_engine.connect() as other:
            other.execute(text("SET lock_timeout = '1s'"))
            unlocked.append(other.execute(text("SELECT id FROM opportunities WHERE id = :id FOR UPDATE NOWAIT"),
                                          {"id": recorded_id}).scalar() == recorded_id)
            other.rollback()
        return real_complete(**kwargs)

    provider.complete = complete
    monkeypatch.setattr("govcon.ai.structured.get_provider", lambda *a, **k: provider)
    recorders = []

    def one_pass():
        recorders.append(active_recorder())
        with session_scope() as s, stable_ids(s):
            lock_opportunity(s, recorded_id)
            return run_compliance_pipeline(s, recorded_id, now=NOW, company_facts=facts)

    recorded = run_recorded(one_pass)
    recorder = recorders[-1]
    assert len(provider.calls) == len(live_provider.calls) > 1, "each AI call is made once"
    assert unlocked and all(unlocked), "no pass held the opportunity lock during an AI call"
    assert recorder.divergences == 0 and not recorder.live, "passes rebuilt identical prompts, ids included"
    assert recorder.passes == len(provider.calls) + 1
    assert recorded["status"] == live["status"] and recorded["counts"] == live["counts"]


def test_recorded_errors_are_replayed_where_the_service_asked():
    from govcon.ai.replay import active_recorder, run_recorded

    performed = []

    def failing():
        performed.append(1)
        raise ValueError("provider down")

    def service():
        try:
            active_recorder().call({"prompt": "x"}, failing)
        except ValueError as exc:
            return f"handled: {exc}"
        return "unreachable"

    assert run_recorded(service) == "handled: provider down"
    assert performed == [1], "the failed call is not repeated by the replay"


def test_inputs_that_keep_changing_finish_in_one_pass_instead_of_looping():
    from itertools import count

    from govcon.ai.replay import active_recorder, run_recorded

    clock = count()
    performed = []

    def service():
        recorder = active_recorder()
        return recorder.call({"now": next(clock)}, lambda: performed.append(1) or "ok"), recorder

    result, recorder = run_recorded(service, max_divergences=2)
    assert result == "ok" and recorder.live and len(performed) == 4


# ── 3. award matches: capped, ranked, one notification per bid ───────────────

def _approver(db):
    user, _ = _make_user(db, f"r3-appr-{uuid4().hex}@example.test", "approver")
    return user.id


def _notifications(db, user_id, opp):
    db.expire_all()
    return db.scalars(select(Notification).where(Notification.user_id == user_id, Notification.opportunity_id == opp.id,
                                                 Notification.notification_type == "outcome_suggested")).all()


@pytest.mark.usefixtures("our_uei")
def test_possible_matches_are_ranked_capped_and_notified_once(db):
    from govcon.learning.award_matching import MAX_POSSIBLE_PER_BID

    approver = _approver(db)
    nsn = unique_nsn()
    opp = submitted_bid(db, nsn=nsn)
    days = [40, 3, 90, 1, 20, 7, 200, 60]
    db.add_all([Award(award_id=f"r3-{d}-{uuid4().hex}", nsn=nsn, recipient_uei=f"VENDOR{d:06d}",
                      awarding_agency="Department of Defense / Defense Logistics Agency",
                      action_date=(SUBMITTED + timedelta(days=d)).date(), raw={}) for d in days])
    db.commit()
    run()
    rows = suggestions(db, opp)
    assert len(rows) == MAX_POSSIBLE_PER_BID and all(r.strength == "possible" for r in rows)
    kept = sorted(int(r.source_ref.split("-")[1]) for r in rows)
    assert kept == sorted(days)[:MAX_POSSIBLE_PER_BID], "the awards nearest after submission are kept"
    [note] = _notifications(db, approver, opp)
    assert note.payload["new_suggestions"] == MAX_POSSIBLE_PER_BID
    db.add(Award(award_id=f"r3-2-{uuid4().hex}", nsn=nsn, recipient_uei="LATEVENDOR01",
                 awarding_agency="Department of Defense / Defense Logistics Agency",
                 action_date=(SUBMITTED + timedelta(days=2)).date(), raw={}))
    db.commit()
    run()
    assert len(suggestions(db, opp)) == MAX_POSSIBLE_PER_BID, "the cap holds across runs"
    assert len(_notifications(db, approver, opp)) == 1


@pytest.mark.usefixtures("our_uei")
def test_a_multi_award_notice_naming_another_firm_does_not_suggest_a_loss(db):
    opp = submitted_bid(db)
    award_notice(db, opp, awardee_uei="OTHERVENDOR1")
    run()
    [first] = suggestions(db, opp)
    assert first.suggested_outcome == "lost", "a single other awardee still suggests a loss"
    award_notice(db, opp, awardee_uei="OTHERVENDOR2")
    run()
    rows = suggestions(db, opp)
    assert len(rows) == 2 and all(r.suggested_outcome is None for r in rows)
    assert all("multi-award" in r.evidence["note"] for r in rows), "the earlier loss suggestion is withdrawn"


@pytest.mark.usefixtures("our_uei")
def test_our_award_in_a_multi_award_still_suggests_won(db):
    opp = submitted_bid(db)
    award_notice(db, opp, awardee_uei="OTHERVENDOR1")
    award_notice(db, opp, awardee_uei=OUR_UEI, name="Our Company")
    run()
    outcomes = sorted((r.awardee_uei, r.suggested_outcome) for r in suggestions(db, opp))
    assert outcomes == [("OTHERVENDOR1", None), (OUR_UEI, "won")]


# ── 4. drafting and AI review use the registration overlay ───────────────────

def _facts_setup(db, tmp_path, *, refreshed_days_ago: int, facts_text: str | None = None):
    uei = f"R3{uuid4().hex[:10].upper()}"
    path = tmp_path / "company_facts.json"
    path.write_text(facts_text if facts_text is not None else json.dumps(
        {"uei": uei, "legal_name": "Facts File LLC", "sam_registration_status": "Active (from the file)"}),
        encoding="utf-8")
    existing = db.get(CompanyRegistration, uei)
    if existing is None:
        db.add(CompanyRegistration(uei=uei, legal_name="SAM Name LLC", registration_status="Expired",
                                   refreshed_at=datetime.now(UTC) - timedelta(days=refreshed_days_ago),
                                   source="sam_entity_api", raw={}))
    opp = new_opportunity(db)
    return opp, Settings(_env_file=None, company_facts_path=path, company_uei=uei)


@pytest.mark.parametrize("age,expected", [(0, "Expired"), (30, None)])
def test_drafting_uses_the_sam_registration_overlay(db, tmp_path, age, expected):
    from govcon.proposals.drafting import _draft_request

    opp, configured = _facts_setup(db, tmp_path, refreshed_days_ago=age)
    request = _draft_request(db, opportunity_id=opp.id, sections_requested=["technical"], company_facts=None,
                             settings=configured)
    company = json.loads(request["variables"]["APPROVED_FACTS_JSON"])["company"]
    assert company.get("sam_registration_status") == expected, "fresh SAM data wins; stale data reads as unknown"
    assert company["_provenance"]["sam_registration"]["stale"] is (expected is None)


def test_ai_review_uses_the_same_overlay(db, tmp_path, monkeypatch):
    from govcon.proposals import ai_review

    opp, configured = _facts_setup(db, tmp_path, refreshed_days_ago=30)
    captured = {}

    def capture(session, **kwargs):
        captured.update(kwargs["variables"])
        raise RuntimeError("stop after capturing the prompt")

    monkeypatch.setattr(ai_review, "run_structured_prompt", capture)
    monkeypatch.setattr(ai_review, "get_sections_for_version", lambda session, version_id: [])
    proposal_version = type("PV", (), {"proposal_id": 1})()
    proposal = type("P", (), {"opportunity_id": opp.id})()
    real_get = db.get
    monkeypatch.setattr(db, "get", lambda model, key, **kw: {"ProposalVersion": proposal_version,
                                                             "Proposal": proposal}.get(model.__name__)
                        or real_get(model, key, **kw))
    with pytest.raises(RuntimeError, match="stop after"):
        ai_review.run_proposal_red_team(db, opportunity_id=opp.id, proposal_version_id=1, settings=configured)
    facts = json.loads(captured["APPROVED_FACTS_JSON"])
    assert "sam_registration_status" not in facts, "stale registration is unknown, not the file's 'Active'"


def test_malformed_company_facts_fail_clearly(db, tmp_path):
    from govcon.compliance.pipeline import CompanyFactsInvalid
    from govcon.proposals.drafting import _draft_request
    from govcon.tasks.errors import classify

    opp, configured = _facts_setup(db, tmp_path, refreshed_days_ago=0, facts_text="{not json")
    with pytest.raises(CompanyFactsInvalid, match="not valid JSON") as raised:
        _draft_request(db, opportunity_id=opp.id, sections_requested=None, company_facts=None, settings=configured)
    outcome = classify(raised.value)
    assert outcome.kind == "fail" and "COMPANY_FACTS_PATH" in outcome.next_action


# ── 5. async routes keep database and file work off the event loop ───────────

def test_upload_and_form_routes_do_no_database_work_on_the_event_loop(db, client, monkeypatch):
    from importlib import import_module

    from govcon.web.routes import (
        accounts,
        common,
        proposals,
        settings,
        sourcing,
    )
    watchlists = import_module("govcon.web.routes.watchlists")

    _, token = _make_user(db, f"r3-owner-{uuid4().hex}@example.test", "owner")
    client.cookies.set("govcon_session", token)
    opp = new_opportunity(db)
    db.add(Pursuit(opportunity_id=opp.id, stage="submitted", submitted_at=datetime.now(UTC)))
    db.commit()
    where: list[str] = []
    real = common.session_scope

    @contextmanager
    def tracking(*args, **kwargs):
        try:
            asyncio.get_running_loop()
            where.append("event loop")
        except RuntimeError:
            where.append("thread")
        with real(*args, **kwargs) as session:
            yield session

    for module in (accounts, common, proposals, settings, sourcing, watchlists):
        monkeypatch.setattr(module, "session_scope", tracking)
    posts = [
        (f"/workspace/{opp.id}/record-outcome", {"data": {"outcome": "no_bid", "no_bid_reason": "r3 probe"}}),
        ("/watchlists/new", {"data": {"name": f"R3 {uuid4().hex[:6]}"}}),
        ("/admin/users/invite", {"data": {"email": f"r3-{uuid4().hex}@example.test", "display_name": "R3",
                                          "password": "a long enough password", "role": "reviewer"}}),
        (f"/workspace/{opp.id}/quotes", {"data": {"supplier_name": f"R3 Supplier {uuid4().hex[:6]}"},
                                         "files": {"quote_file": ("q.csv", b"description,quantity,unit_price\nGloves,10,2.50\n",
                                                                  "text/csv")}}),
        ("/suppliers", {"data": {"name": f"R3 Catalog {uuid4().hex[:6]}"},
                        "files": {"catalog_file": ("c.csv", b"part_number,description,unit_price\nA1,Gloves,2.50\n",
                                                   "text/csv")}}),
    ]
    for url, kwargs in posts:
        where.clear()
        response = client.post(url, **kwargs)
        assert response.status_code in (200, 303), (url, response.status_code)
        assert where and "event loop" not in where, (url, where)


# ── 6. pursuit commercial facts on the web ────────────────────────────────────

def _pursuit(db, stage="evaluating"):
    opp = new_opportunity(db)
    pursuit = Pursuit(opportunity_id=opp.id, stage=stage)
    db.add(pursuit)
    db.commit()
    return opp, pursuit


def _facts_form(pursuit, **values):
    form = {"expected_version": str(pursuit.version), "quote_price": "", "sourcing_cost": "", "supplier": "",
            "notes": "", "return_tab": "pricing"}
    form.update(values)
    return form


def test_a_reviewer_records_commercial_facts_on_the_web(db, client, monkeypatch):
    from govcon.workflow import commercial

    _, token = _make_user(db, f"r3-rev-{uuid4().hex}@example.test", "reviewer")
    client.cookies.set("govcon_session", token)
    opp, pursuit = _pursuit(db)
    version = pursuit.version
    page = client.get(f"/workspace/{opp.id}?tab=products").text
    assert f'action="/workspace/{opp.id}/pursuit-facts"' in page
    invalidated = []
    monkeypatch.setattr(commercial, "invalidate_commercial_decisions",
                        lambda session, opportunity_id, *, actor: invalidated.append(opportunity_id))
    response = client.post(f"/workspace/{opp.id}/pursuit-facts",
                           data=_facts_form(pursuit, quote_price="$1,250.00", sourcing_cost="1000", supplier="Acme"))
    assert "notice=" in response.headers["location"] and "tab=pricing" in response.headers["location"]
    db.expire_all()
    saved = db.get(Pursuit, pursuit.id)
    assert (saved.quote_price, saved.sourcing_cost, saved.supplier) == (Decimal("1250.00"), Decimal(1000), "Acme")
    assert saved.version == version + 1 and invalidated == [opp.id]
    audit = db.scalar(select(AuditEvent).where(AuditEvent.opportunity_id == opp.id,
                                               AuditEvent.action_type == "pursuit_updated"))
    assert audit.new_value["via"] == "web" and audit.new_value["quote_price"] == "1250.00"
    stale = client.post(f"/workspace/{opp.id}/pursuit-facts",
                        data={**_facts_form(saved, quote_price="1"), "expected_version": str(version)})
    assert "error=" in stale.headers["location"], "a stale form cannot overwrite newer facts"
    cleared = client.post(f"/workspace/{opp.id}/pursuit-facts", data=_facts_form(saved, supplier="Acme"))
    assert "notice=" in cleared.headers["location"]
    db.expire_all()
    assert db.get(Pursuit, pursuit.id).quote_price is None, "a blank field clears it"


@pytest.mark.parametrize("case", ["read_only", "submitted", "negative"])
def test_commercial_facts_form_refusals(db, client, case):
    role = "read_only" if case == "read_only" else "reviewer"
    _, token = _make_user(db, f"r3-{case}-{uuid4().hex}@example.test", role)
    client.cookies.set("govcon_session", token)
    opp, pursuit = _pursuit(db, stage="submitted" if case == "submitted" else "evaluating")
    response = client.post(f"/workspace/{opp.id}/pursuit-facts",
                           data=_facts_form(pursuit, quote_price="-5" if case == "negative" else "10"))
    assert "error=" in response.headers["location"]
    db.expire_all()
    assert db.get(Pursuit, pursuit.id).quote_price is None
