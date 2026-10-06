"""Roadmap stage 1C: market / supplier / pricing analyses run as durable tasks (ADR-064)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from test_intelligence_analyses import FakeProvider, _opp, _token
from web_client import CsrfTestClient

from govcon.ai.analysis_types import AnalysisType
from govcon.models import AIAnalysis, Pursuit, Task
from govcon.tasks.testing import drain
from govcon.web.app import create_app


@pytest.fixture(autouse=True)
def synced_prompts(upgraded_engine):
    """Activate the registry prompts; this file may run before the suite's bootstrap test.

    Per test, so it runs after conftest relaxes the behavioral-evaluation gate.
    """
    from pathlib import Path

    from govcon.prompting.registry import sync_prompts
    with Session(upgraded_engine) as session:
        sync_prompts(session, Path(__file__).parent.parent / "src" / "govcon" / "prompts")
        session.commit()


@pytest.fixture()
def db(upgraded_engine):
    with Session(upgraded_engine) as session:
        yield session


@pytest.fixture()
def client(upgraded_engine):
    with CsrfTestClient(create_app(), follow_redirects=False) as client:
        yield client


def tasks_for(db, opp_id):
    db.expire_all()
    return db.scalars(select(Task).where(Task.opportunity_id == opp_id, Task.task_type == "ai_analysis")
                      .order_by(Task.id)).all()


def analyses(db, opp_id, analysis_type):
    db.expire_all()
    return db.scalars(select(AIAnalysis).where(AIAnalysis.opportunity_id == opp_id,
                                               AIAnalysis.analysis_type == analysis_type)).all()


def test_request_queues_and_worker_runs_the_analysis(db, client):
    opp = _opp(db)
    db.commit()
    token = _token(db, "reviewer")
    with patch("govcon.ai.structured.get_provider", return_value=FakeProvider()):
        first = client.post(f"/workspace/{opp.id}/analyze/market", cookies={"govcon_session": token})
        assert first.status_code == 303 and "notice=" in first.headers["location"]
        again = client.post(f"/workspace/{opp.id}/analyze/market", cookies={"govcon_session": token})
        assert "already queued" in client.get(again.headers["location"], cookies={"govcon_session": token}).text
        [task] = tasks_for(db, opp.id)
        assert task.status == "queued" and task.payload == {"kind": "market"}
        assert analyses(db, opp.id, AnalysisType.MARKET) == [], "the request does not call the AI"
        page = client.get(f"/workspace/{opp.id}?tab=market", cookies={"govcon_session": token}).text
        assert "Analysis queued" in page
        assert drain(opportunity_id=opp.id) == [(task.id, "succeeded")]
    [analysis] = analyses(db, opp.id, AnalysisType.MARKET)
    assert tasks_for(db, opp.id)[0].result == {"kind": "market", "analysis_id": analysis.id}
    page = client.get(f"/workspace/{opp.id}?tab=market", cookies={"govcon_session": token}).text
    assert "DLA buys quarterly" in page and "Analysis queued" not in page


def test_missing_inputs_are_refused_before_queueing(db, client):
    opp = _opp(db)
    db.add(Pursuit(opportunity_id=opp.id, stage="sourcing"))
    db.commit()
    token = _token(db, "reviewer")
    response = client.post(f"/workspace/{opp.id}/analyze/supplier", cookies={"govcon_session": token})
    assert "supplier" in client.get(response.headers["location"], cookies={"govcon_session": token}).text
    assert tasks_for(db, opp.id) == []


def test_inputs_removed_after_queueing_wait_for_the_approver(allow_proprietary_ai, db, client):
    opp = _opp(db)
    db.add(Pursuit(opportunity_id=opp.id, stage="sourcing", supplier="Acme Gloves", sourcing_cost=Decimal("150")))
    db.commit()
    token = _token(db, "reviewer")
    with patch("govcon.ai.structured.get_provider", return_value=FakeProvider()):
        client.post(f"/workspace/{opp.id}/analyze/supplier", cookies={"govcon_session": token})
        [task] = tasks_for(db, opp.id)
        # The facts the task was queued for change, then disappear.
        db.execute(update(Pursuit).where(Pursuit.opportunity_id == opp.id).values(supplier=None, sourcing_cost=None))
        db.commit()
        results = drain(opportunity_id=opp.id)
    assert [status for _, status in results] == ["cancelled", "waiting_for_input"]
    old, new = tasks_for(db, opp.id)
    assert old.superseded_by_task_id == new.id
    assert new.blocker_owner_role == "approver" and "Pursuit" in new.blocker_next_action
    page = client.get(f"/workspace/{opp.id}?tab=products", cookies={"govcon_session": token}).text
    assert "Supplier analysis is waiting for input" in page and new.blocker_next_action in page
    assert analyses(db, opp.id, AnalysisType.SOURCING) == []


def test_policy_block_is_reported_at_once(db, client):
    opp = _opp(db)
    db.add(Pursuit(opportunity_id=opp.id, stage="sourcing", quote_price=Decimal("200"), sourcing_cost=Decimal("150")))
    db.commit()
    token = _token(db, "reviewer")
    response = client.post(f"/workspace/{opp.id}/analyze/pricing", cookies={"govcon_session": token})
    assert "blocked_by_policy" in client.get(response.headers["location"], cookies={"govcon_session": token}).text
    assert tasks_for(db, opp.id) == []


def test_cli_queues_and_runs_the_analysis(db):
    from typer.testing import CliRunner

    from govcon.cli import app
    opp = _opp(db)
    db.commit()
    with patch("govcon.ai.structured.get_provider", return_value=FakeProvider()):
        result = CliRunner().invoke(app, ["enrich", "intelligence", "--opportunity-id", str(opp.id), "--kind", "market"])
    assert result.exit_code == 0, result.output
    [analysis] = analyses(db, opp.id, AnalysisType.MARKET)
    assert f"analysis_id: {analysis.id}" in result.output
    assert tasks_for(db, opp.id)[0].status == "succeeded"
