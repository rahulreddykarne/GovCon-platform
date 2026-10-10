"""Stuck ingest: hard step limits, the stale-run reaper, and "Run ingest now" on /ops."""

from __future__ import annotations

import secrets
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from web_client import CsrfTestClient

from govcon.collaboration.users import create_session, hash_password
from govcon.config import get_settings
from govcon.db import make_engine, session_scope
from govcon.models import IngestionRun, SchedulerJobRun, Task, User
from govcon.ops.reaper import held_chain_locks, reap_stale_runs
from govcon.scheduler import chain_tasks, chains
from govcon.scheduler.chain_tasks import CHAIN_TASK, chain_task_failed, queue_chain
from govcon.scheduler.jobs import StepResult
from govcon.scheduler.limits import chain_max_seconds
from govcon.tasks.worker import run_once

pytestmark = pytest.mark.usefixtures("upgraded_engine")


@pytest.fixture()
def slow_chain(monkeypatch):
    """A chain whose ``slow`` step writes a row, then blocks until released."""
    release = threading.Event()
    marker = f"timeout-step-{secrets.token_hex(4)}"

    def slow(session):
        session.add(IngestionRun(job=marker, started_at=datetime.now(UTC), status="running"))
        session.flush()
        release.wait(20)
        return StepResult(step="slow", status="succeeded")

    def after(session):
        return StepResult(step="after", status="succeeded")

    name = f"timeout_test_{secrets.token_hex(4)}"
    monkeypatch.setitem(chains.CHAIN_DEFINITIONS, name, chains.ChainDef(
        name=name, description="test", cron="manual", steps=["slow", "after"], soft_steps=frozenset({"slow"})))
    monkeypatch.setattr(chains, "_STEP_FUNCTIONS", {**chains._STEP_FUNCTIONS, "slow": slow, "after": after})
    monkeypatch.setattr(chain_tasks, "step_timeout", lambda step, settings: 1)
    yield name, marker
    release.set()


def test_a_step_past_its_limit_is_stopped_rolled_back_and_recorded(slow_chain):
    chain_name, marker = slow_chain
    with session_scope() as db:
        task, _ = queue_chain(db, chain_name, trigger="test", slot="timeout")
        task_id = task.id
    assert run_once(task_id=task_id) == (task_id, "succeeded")
    with session_scope() as db:
        task = db.get(Task, task_id)
        run = db.get(SchedulerJobRun, task.scheduler_job_run_id)
        assert run.status == "completed_with_errors", "a soft step that timed out does not stop the chain"
        assert "slow: timed out after 1 s" in run.error
        assert run.steps_completed == ["after"]
        assert db.scalar(select(IngestionRun).where(IngestionRun.job == marker)) is None, \
            "the timed-out step's uncommitted work was rolled back"


def _running_row(db: Session, chain_name: str, age: timedelta) -> SchedulerJobRun:
    row = SchedulerJobRun(chain_name=chain_name, trigger="test", status="running",
                          started_at=datetime.now(UTC) - age, steps_completed=[])
    db.add(row)
    db.flush()
    return row


def test_reaper_fails_a_run_past_its_maximum_and_frees_the_chain_lock():
    settings = get_settings()
    key = chains._chain_lock_key("morning_ingest")
    engine = make_engine()
    try:
        holder = engine.connect()
        holder.execute(text("SELECT pg_advisory_lock(742901, :key)"), {"key": key})
        holder.commit()
        with session_scope() as db:
            age = timedelta(seconds=chain_max_seconds("morning_ingest", settings) + 3600)
            run_id = _running_row(db, "morning_ingest", age).id
            assert key in held_chain_locks(db)
            reaped = reap_stale_runs(db, settings)
        assert run_id in [r.run_id for r in reaped]
        with session_scope() as db:
            run = db.get(SchedulerJobRun, run_id)
            assert run.status == "failed" and run.finished_at is not None
            assert "past this chain's maximum" in run.error
            assert "stopped 1 database session" in run.error
        with pytest.raises(DBAPIError):
            holder.execute(text("SELECT 1"))
        holder.invalidate()
    finally:
        engine.dispose()


def test_reaper_fails_a_run_whose_worker_is_gone_but_keeps_a_resumable_one():
    settings = get_settings()
    with session_scope() as db:
        orphan = _running_row(db, "midday_check", timedelta(minutes=10))
        resumable = _running_row(db, "sunday_sweep", timedelta(minutes=10))
        task, _ = queue_chain(db, "sunday_sweep", trigger="test", slot=f"resume-{secrets.token_hex(3)}")
        task.status, task.attempts, task.scheduler_job_run_id = "running", 1, resumable.id
        young = _running_row(db, "usaspending", timedelta(seconds=10))
        ids = orphan.id, resumable.id, young.id
        reap_stale_runs(db, settings)
    with session_scope() as db:
        orphan, resumable, young = (db.get(SchedulerJobRun, i) for i in ids)
        assert orphan.status == "failed" and "worker running this chain stopped" in orphan.error
        assert resumable.status == "running", "a worker will resume it from its checkpoint"
        assert young.status == "running", "a run that just started may not hold its lock yet"
        for row in (resumable, young):
            row.status = "failed"
        db.execute(text("UPDATE tasks SET status = 'cancelled' WHERE scheduler_job_run_id = :id"),
                   {"id": resumable.id})


def test_a_failed_chain_task_closes_its_run_row():
    with session_scope() as db:
        run = _running_row(db, "embeddings", timedelta(minutes=1))
        task, _ = queue_chain(db, "embeddings", trigger="test", slot=f"fail-{secrets.token_hex(3)}")
        task.scheduler_job_run_id, task.current_step = run.id, "semantic_match"
        task.status, task.last_error = "failed", "OperationalError: server closed the connection"
        chain_task_failed(db, task)
        assert run.status == "failed" and run.failed_step == "semantic_match"
        assert "server closed the connection" in run.error


def _login(role: str) -> dict[str, str]:
    with Session(make_engine()) as db:
        user = User(email=f"{role}-{secrets.token_hex(4)}@example.test", display_name=role,
                    password_hash=hash_password("TestPassword123!"), role=role, is_active=True)
        db.add(user)
        db.flush()
        raw = create_session(db, user)
        db.commit()
    return {"govcon_session": raw}


@pytest.fixture()
def web():
    from govcon.web.app import create_app

    get_settings.cache_clear()
    with CsrfTestClient(create_app(), raise_server_exceptions=True, follow_redirects=False) as client:
        yield client


def _active_chain_tasks(chain_name: str) -> list[Task]:
    with session_scope() as db:
        return [t for t in db.scalars(select(Task).where(
            Task.task_type == CHAIN_TASK, Task.status.in_(("queued", "running", "retrying"))))
            if (t.payload or {}).get("chain_name") == chain_name]


def test_run_ingest_now_reaps_the_stuck_run_and_queues_a_real_job(web):
    for task in _active_chain_tasks("morning_ingest") + _active_chain_tasks("evening_ingest"):
        with session_scope() as db:
            db.get(Task, task.id).status = "cancelled"
    settings = get_settings()
    with session_scope() as db:
        age = timedelta(seconds=chain_max_seconds("morning_ingest", settings) + 600)
        stuck_id = _running_row(db, "morning_ingest", age).id
    cookies = _login("owner")
    response = web.post("/ops/jobs/morning_ingest/run", cookies=cookies)
    assert response.status_code == 303 and response.headers["location"] == "/ops?notice=1"
    queued = _active_chain_tasks("morning_ingest")
    assert len(queued) == 1 and queued[0].payload["trigger"] == "manual"
    with session_scope() as db:
        assert db.get(SchedulerJobRun, stuck_id).status == "failed"
    page = web.get("/ops", cookies=cookies)
    assert "Marked 1 stuck run(s) failed" in page.text and "No email is sent" in page.text

    again = web.post("/ops/jobs/evening_ingest/run", cookies=cookies)
    assert again.status_code == 303
    assert "already queued as task" in web.get("/ops", cookies=cookies).text, \
        "morning and evening share one lock group"
    assert len(_active_chain_tasks("evening_ingest")) == 0
    with session_scope() as db:
        db.get(Task, queued[0].id).status = "cancelled"


def test_run_now_needs_approve_permission_and_a_known_job(web):
    reviewer = _login("reviewer")
    denied = web.post("/ops/jobs/morning_ingest/run", cookies=reviewer)
    assert denied.status_code == 303
    assert "Not permitted" in web.get("/ops", cookies=reviewer).text
    assert web.post("/ops/jobs/not_a_chain/run", cookies=_login("owner")).status_code == 404
