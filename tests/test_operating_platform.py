"""Operating views and the quality gate. No test here calls a live API."""

from __future__ import annotations

from uuid import uuid4

from pydantic import BaseModel, Field
from test_web_ui import _make_user

from govcon.ai.quality import assess_output_quality
from govcon.ai.schemas import SolicitationAnalysisV1
from govcon.bots.status import analysis_labels
from govcon.config import Settings
from govcon.scheduler.jobs import step_source_documents


class _Sparse(BaseModel):
    note: str = "ok"
    source_refs: list[dict] = Field(default_factory=list)


def test_sparse_schema_valid_output_is_incomplete() -> None:
    quality, reason = assess_output_quality(SolicitationAnalysisV1(summary="ok"))
    assert quality == "incomplete"
    assert reason
    quality, reason = assess_output_quality(_Sparse())
    assert quality == "incomplete"


def test_cited_output_is_accepted() -> None:
    analysis = SolicitationAnalysisV1(
        summary="The notice asks for twelve valve kits delivered to Norfolk.",
        source_refs=[{"quote": "Deliver twelve valve kits to Norfolk.", "section": "Section B"}],
    )
    quality, reason = assess_output_quality(analysis)
    assert quality == "accepted" and reason == ""


def test_analysis_label_is_not_run_without_a_bot_run(db) -> None:
    labels = analysis_labels(db, [424242])
    assert labels[424242]["label"] == "not run"


def test_source_documents_does_nothing_without_a_recent_ingest(db, monkeypatch) -> None:
    from datetime import datetime as real_datetime

    class FarFuture(real_datetime):
        @classmethod
        def now(cls, tz=None):
            moment = real_datetime(2099, 6, 1, tzinfo=tz)
            return moment

    # Other tests leave recent ingest rows in this database. A clock past all of them
    # is the same condition as a laptop with no ingest in the last six hours.
    monkeypatch.setattr("datetime.datetime", FarFuture)
    result = step_source_documents(db, Settings(_env_file=None))
    assert result.status == "succeeded"
    assert result.extra == {"reason": "no recent ingest"}


def test_settings_page_renders_the_operator_clocks(db, client) -> None:
    _, token = _make_user(db, f"sched-{uuid4().hex[:8]}@example.test", "owner")
    db.commit()
    client.cookies.set("govcon_session", token)
    page = client.get("/settings").text
    assert "When the scheduler runs" in page
    assert 'name="schedule_morning_ingest" value="06:30"' in page
    assert 'name="schedule_evening_ingest" value="17:00"' in page
    assert "External AI sharing" in page
    assert "restart it after a change" in page


def test_operate_views_use_stored_rows(db, client) -> None:
    _, token = _make_user(db, f"ops-{uuid4().hex[:8]}@example.test", "owner")
    db.commit()
    client.cookies.set("govcon_session", token)
    overview = client.get("/operate")
    assert overview.status_code == 200
    text = overview.text
    assert "Mission control" in text
    assert "No bot approval is pending." in text
    assert "DLA aircraft parts" not in text
    agents = client.get("/operate/agents")
    assert agents.status_code == 200 and "Orchestrator" in agents.text and "No stored run" in agents.text
    architecture = client.get("/operate/architecture?node=sam")
    assert architecture.status_code == 200 and "SAM.gov" in architecture.text and "→" in architecture.text
    integrations = client.get("/operate/integrations")
    assert integrations.status_code == 200
    assert "Not configured" in integrations.text
    assert "Public endpoint, no key" in integrations.text
    unknown = client.get("/operate/not-a-view", follow_redirects=False)
    assert unknown.status_code == 303 and unknown.headers["location"] == "/operate"
