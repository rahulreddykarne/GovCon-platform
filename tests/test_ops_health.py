"""Ops health is local: heartbeats, chain board, and no secret leakage."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from govcon.config import Settings
from govcon.models import SchedulerJobRun
from govcon.ops.health import chain_board, collect_health, record_heartbeat


@pytest.fixture()
def session(upgraded_engine) -> Session:
    with Session(upgraded_engine) as db:
        yield db
        db.rollback()


def test_health_reports_a_fresh_worker_and_hides_secrets(session: Session) -> None:
    record_heartbeat(session, role="worker", instance_id="worker-1", detail={"note": "local"})
    session.flush()
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://govcon:govcon@127.0.0.1/govcon",
        deepseek_api_key="sk-test-secret-value",
        ai_external_allowed_for_proprietary=False,
    )
    report = collect_health(session, settings)
    assert report["status"] == "ok"
    by_name = {item["name"]: item for item in report["checks"]}
    assert by_name["database"]["status"] == "ok"
    assert by_name["worker"]["status"] == "ok"
    assert by_name["scheduler"]["status"] == "down"
    assert "sk-test-secret-value" not in str(report)
    assert "configured: deepseek" in by_name["ai_providers"]["detail"]
    assert "proprietary=off" in by_name["ai_providers"]["detail"]


def test_chain_board_shows_last_failure_and_an_action(session: Session) -> None:
    session.add(SchedulerJobRun(
        chain_name="morning_ingest",
        trigger="scheduler",
        started_at=datetime(2099, 1, 1, tzinfo=UTC),
        finished_at=datetime(2099, 1, 1, 0, 1, tzinfo=UTC),
        status="failed",
        failed_step="sam_ingest",
        error="SAM HTTP 503",
        steps_completed=[],
        row_counts={"sam_ingest": {"fetched": 0, "inserted": 0, "updated": 0, "unchanged": 0}},
    ))
    session.flush()
    board = {row["name"]: row for row in chain_board(session)}
    morning = board["morning_ingest"]
    assert morning["cron"] == "11:30 PM America/Los_Angeles"
    assert morning["last_failure"] is not None
    assert morning["last_failure"].error == "SAM HTTP 503"
    assert "govcon jobs run morning_ingest" in morning["action"]
    assert morning["duration_text"] == "60s"
    assert "not scheduled" in morning["next_run_local"]
