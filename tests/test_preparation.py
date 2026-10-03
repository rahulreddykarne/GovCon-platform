"""Roadmap gap 4 (stage 2D): one pursuit service and automatic preparation (ADR-067)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from govcon.db import session_scope
from govcon.models import AppSetting, AuditEvent, Match, Opportunity, Pursuit, ReviewAssignment, ReviewSession, Task, Watchlist
from govcon.tasks.testing import drain
from govcon.web.app import create_app
from govcon.workflow.preparation import PREPARATION_TASK, STEPS
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


@pytest.fixture(autouse=True)
def default_settings(upgraded_engine):
    """Each test starts from the default settings and restores them afterwards."""
    with session_scope() as s:
        s.query(AppSetting).delete()
    yield
    with session_scope() as s:
        s.query(AppSetting).delete()


def new_opportunity(db):
    opp = Opportunity(source="sam", source_id=uuid4().hex, title=f"Prep {uuid4().hex[:6]}", status="open",
                      response_deadline=datetime.now(UTC) + timedelta(days=30), raw={}, links={})
    db.add(opp)
    db.commit()
    return opp


def state(db, opp_id):
    db.expire_all()
    pursuits = db.scalars(select(Pursuit).where(Pursuit.opportunity_id == opp_id)).all()
    created = db.scalars(select(AuditEvent).where(AuditEvent.opportunity_id == opp_id,
                                                  AuditEvent.action_type == "pursuit_created")).all()
    tasks = db.scalars(select(Task).where(Task.opportunity_id == opp_id, Task.task_type == PREPARATION_TASK)).all()
    return pursuits, created, tasks


def _set(db, owner, **values):
    from govcon.workflow.app_settings import set_setting
    with session_scope() as s:
        actor = s.get(type(owner), owner.id)
        for key, value in values.items():
            set_setting(s, key, value, actor=actor)


# ── one service, same result from every entry point ──────────────────────────

@pytest.mark.parametrize("entry", ["inbox", "workspace", "cli", "mcp"])
def test_every_entry_point_creates_the_same_pursuit_and_preparation(db, client, monkeypatch, entry):
    opp = new_opportunity(db)
    user, token = _make_user(db, f"prep-{uuid4().hex}@example.test", "owner")
    client.cookies.set("govcon_session", token)
    for _ in range(2):  # a repeat changes nothing
        if entry == "inbox":
            wl = db.scalar(select(Watchlist).limit(1)) or Watchlist(name=f"WL {uuid4().hex}", enabled=True)
            db.add(wl)
            db.flush()
            match = db.scalar(select(Match).where(Match.opportunity_id == opp.id)) or Match(
                opportunity_id=opp.id, watchlist_id=wl.id, status="new", active=True)
            db.add(match)
            db.commit()
            assert client.post("/inbox/action", data={"match_id": match.id, "action": "pursuing"}).status_code == 200
        elif entry == "workspace":
            assert client.post(f"/opp/{opp.id}/start-workspace").status_code == 303
        elif entry == "cli":
            from typer.testing import CliRunner
            from govcon.cli import app
            result = CliRunner().invoke(app, ["pursuit", "start", "--opportunity-id", str(opp.id), "--actor-email", user.email])
            assert result.exit_code == 0, result.output
        else:
            from govcon.mcp import operations as mcp_ops
            from govcon.mcp.context import reset_server_actor
            reset_server_actor()
            monkeypatch.setenv("MCP_ACTOR_EMAIL", user.email)
            with session_scope() as s:
                response = mcp_ops.op_add_pursuit(s, opp.id)
            assert response["ok"] is True and response["data"]["stage"] == "evaluating"
            reset_server_actor()
    pursuits, created, tasks = state(db, opp.id)
    assert [p.stage for p in pursuits] == ["evaluating"]
    assert len(created) == 1 and created[0].user_id == user.id
    assert created[0].new_value["via"] == {"inbox": "web_inbox", "workspace": "web_workspace", "cli": "cli", "mcp": "mcp"}[entry]
    assert [t.status for t in tasks] == ["queued"]


def test_mcp_repeat_returns_the_existing_pursuit(db, monkeypatch):
    from govcon.mcp import operations as mcp_ops
    from govcon.mcp.context import reset_server_actor
    opp = new_opportunity(db)
    user, _ = _make_user(db, f"mcp-{uuid4().hex}@example.test", "owner")
    reset_server_actor()
    monkeypatch.setenv("MCP_ACTOR_EMAIL", user.email)
    try:
        with session_scope() as s:
            first = mcp_ops.op_add_pursuit(s, opp.id)
        with session_scope() as s:
            again = mcp_ops.op_add_pursuit(s, opp.id)
    finally:
        reset_server_actor()
    assert first["data"]["created"] is True and again["data"]["created"] is False
    assert first["data"]["pursuit_id"] == again["data"]["pursuit_id"]


def test_concurrent_starts_create_one_pursuit_and_one_preparation(db):
    from govcon.models import User
    from govcon.workflow.pursuits import create_or_get_pursuit
    opp = new_opportunity(db)
    user, _ = _make_user(db, f"race-{uuid4().hex}@example.test", "reviewer")
    user_id, opp_id = user.id, opp.id  # threads must not touch the test's session
    barrier = Barrier(4)

    def start():
        barrier.wait(timeout=10)
        with session_scope() as s:
            return create_or_get_pursuit(s, opportunity_id=opp_id, actor=s.get(User, user_id), origin="web_inbox")[1]

    with ThreadPoolExecutor(max_workers=4) as pool:
        created = [f.result(timeout=60) for f in [pool.submit(start) for _ in range(4)]]
    assert sorted(created) == [False, False, False, True]
    pursuits, audits, tasks = state(db, opp.id)
    assert len(pursuits) == len(audits) == len(tasks) == 1


def test_disabled_auto_preparation_queues_nothing(db, client):
    opp = new_opportunity(db)
    owner, token = _make_user(db, f"off-{uuid4().hex}@example.test", "owner")
    _set(db, owner, auto_prepare_on_pursuit={"enabled": False})
    client.cookies.set("govcon_session", token)
    client.post(f"/opp/{opp.id}/start-workspace")
    pursuits, _, tasks = state(db, opp.id)
    assert len(pursuits) == 1 and tasks == []
    page = client.get(f"/workspace/{opp.id}?tab=overview").text
    assert "has not been prepared automatically" in page and f'action="/workspace/{opp.id}/prepare"' in page
    assert "notice=" in client.post(f"/workspace/{opp.id}/prepare").headers["location"]
    assert [t.status for t in state(db, opp.id)[2]] == ["queued"]


# ── the preparation run ──────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["named", "all_active", "manual"])
def test_preparation_runs_every_step_and_assigns_reviewers_by_setting(db, client, mode):
    opp = new_opportunity(db)
    owner, token = _make_user(db, f"own-{uuid4().hex}@example.test", "owner")
    named, _ = _make_user(db, f"named-{uuid4().hex}@example.test", "reviewer")
    value = {"mode": mode, "user_ids": [named.id] if mode == "named" else []}
    _set(db, owner, reviewer_assignment=value)
    client.cookies.set("govcon_session", token)
    client.post(f"/opp/{opp.id}/start-workspace")
    [(task_id, status)] = drain(opportunity_id=opp.id, task_types=[PREPARATION_TASK])
    assert status == "succeeded"
    task = db.get(Task, task_id)
    assert task.checkpoint["completed_steps"] == list(STEPS)
    assert db.scalar(select(ReviewSession).where(ReviewSession.opportunity_id == opp.id)) is not None
    assigned = set(db.scalars(select(ReviewAssignment.user_id).where(ReviewAssignment.opportunity_id == opp.id)))
    if mode == "named":
        assert assigned == {named.id}
    elif mode == "all_active":
        assert named.id in assigned
    else:
        assert assigned == set() and "manual" in task.checkpoint["data"]["review"]["note"]
    analyses = task.checkpoint["data"]["research"]["analyses"]
    assert set(analyses) == {"market", "supplier", "pricing"}
    assert "waiting for input" in analyses["supplier"], "missing pursuit facts never stop preparation"
    page = client.get(f"/workspace/{opp.id}?tab=overview").text
    assert "Download and read documents" in page and "Decision package" in page and "Re-run preparation" in page


# ── the Settings page ────────────────────────────────────────────────────────

def test_owner_changes_settings_and_others_cannot(db, client):
    owner, owner_token = _make_user(db, f"set-own-{uuid4().hex}@example.test", "owner")
    reviewer, reviewer_token = _make_user(db, f"set-rev-{uuid4().hex}@example.test", "reviewer")
    client.cookies.set("govcon_session", owner_token)
    page = client.get("/settings").text
    assert "Reviewer assignment during preparation" in page and "Save settings" in page
    saved = client.post("/settings", data={"reviewer_mode": "named", "reviewer_ids": [str(reviewer.id)], "auto_prepare": "on"})
    assert "notice=" in saved.headers["location"]
    db.expire_all()
    assert db.get(AppSetting, "reviewer_assignment").value == {"mode": "named", "user_ids": [reviewer.id]}
    assert db.scalar(select(func.count()).select_from(AuditEvent).where(
        AuditEvent.action_type == "app_setting_changed", AuditEvent.user_id == owner.id)) >= 2
    empty = client.post("/settings", data={"reviewer_mode": "named", "auto_prepare": "on"})
    assert "error=" in empty.headers["location"], "named assignment needs at least one reviewer"

    client.cookies.set("govcon_session", reviewer_token)
    assert "Only an owner" in client.get("/settings").text
    denied = client.post("/settings", data={"reviewer_mode": "manual"})
    assert "error=" in denied.headers["location"]
    db.expire_all()
    assert db.get(AppSetting, "reviewer_assignment").value["mode"] == "named"
