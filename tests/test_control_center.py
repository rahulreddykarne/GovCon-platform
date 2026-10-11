"""Isolated read-model and render checks; no live providers or production database."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import JSON, BigInteger, Column, Integer, MetaData, Table, create_engine
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Session

from govcon.config import Settings
from govcon.display_time import format_pt
from govcon.models import (
    AIProviderCall,
    BotRun,
    FilePage,
    SchedulerJobRun,
    StoredFile,
    Task,
)
from govcon.operating.console import control_center


@pytest.fixture()
def console_db(monkeypatch):
    # Exercise the projections with real SQL and mapped rows in a disposable SQLite DB.
    # This is not a substitute for PostgreSQL migration or lifecycle tests.
    metadata = MetaData()
    for model in (Task, AIProviderCall, BotRun, SchedulerJobRun, StoredFile, FilePage):
        columns = []
        for column in model.__table__.columns:
            kind = column.type
            if isinstance(kind, (JSONB, ARRAY)):
                kind = JSON()
            elif isinstance(kind, BigInteger):
                kind = Integer()
            columns.append(Column(column.name, kind, primary_key=column.primary_key, nullable=True))
        Table(model.__tablename__, metadata, *columns)
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    monkeypatch.setattr("govcon.operating.console.collect_health", lambda *args: {
        "checks": [{"name": "worker", "status": "down", "detail": "No heartbeat recorded"}],
    })
    monkeypatch.setattr("govcon.operating.console.chain_board", lambda *args: [])
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_counts_cost_unknown_and_stale_lease(console_db):
    now = datetime.now(UTC)
    console_db.add_all([
        Task(id=1, task_type="opportunity_preparation", status="running", dedup_key="run",
             created_at=now, updated_at=now, started_at=now, next_attempt_at=now,
             lease_expires_at=now - timedelta(minutes=2), current_step="documents", attempts=1,
             max_attempts=5, checkpoint={}, payload={}, input_revision={}),
        AIProviderCall(id=1, provider="deepseek", purpose="summary", status="succeeded",
                       cost_usd="0.1234", created_at=now),
        AIProviderCall(id=2, provider="jev", purpose="decision", status="failed", created_at=now),
        AIProviderCall(id=3, provider="local", purpose="embedding", status="local", cost_usd=0, created_at=now),
    ])
    console_db.flush()
    data = control_center(console_db, Settings(_env_file=None), view="overview")
    assert data["running"] == 1
    assert data["call_count"] == 2 and data["unknown_cost_count"] == 1
    assert data["known_cost"] == "$0.1234"
    assert data["tasks"][0]["step"] == "documents" and data["tasks"][0]["lease_expired"]
    assert next(p for p in data["providers"] if p["name"] == "jev")["statuses"] == {"failed": 1}
    assert next(p for p in data["providers"] if p["name"] == "openai")["calls"] == 0


def test_schedule_pagination_and_filter_are_exact(console_db):
    now = datetime.now(UTC)
    console_db.add_all([SchedulerJobRun(id=i, chain_name="morning_ingest", trigger="scheduled",
                         status="succeeded", started_at=now, finished_at=now,
                         steps_completed=["sam_ingest"]) for i in range(1, 36)])
    console_db.add(SchedulerJobRun(id=36, chain_name="evening_ingest", trigger="manual",
                                 status="failed", started_at=now, error="api_key=secret-value"))
    console_db.flush()
    data = control_center(console_db, Settings(_env_file=None), view="schedules", page=999,
                          chain="morning_ingest")
    assert data["schedule_total"] == 36
    assert data["page"] == 2 and data["pages"] == 2 and len(data["runs"]) == 5
    assert all(row["name"] == "morning_ingest" for row in data["runs"])
    failed = control_center(console_db, Settings(_env_file=None), view="schedules", chain="evening_ingest")
    assert "secret-value" not in failed["runs"][0]["error"]


def test_document_reading_trace_omits_contents_and_paths(console_db):
    now = datetime.now(UTC)
    console_db.add(StoredFile(id=7, opportunity_id=42, filename="Valve specs.pdf", active=True,
        created_at=now, updated_at=now, classification="PUBLIC", source_origin="sam_attachment",
        extraction_status="partial", page_count=3, ocr_pages=[2], ocr_failed_pages=[3],
        extracted_text="DO NOT COPY DOCUMENT CONTENT", local_path="private-local-path"))
    console_db.add_all([
        FilePage(id=1, file_id=7, page_no=1, text_source="native", text="DO NOT COPY", char_count=220),
        FilePage(id=2, file_id=7, page_no=2, text_source="ocr", text="DO NOT COPY", char_count=150,
                 ocr_confidence=82),
        FilePage(id=3, file_id=7, page_no=3, text_source="none", text="", char_count=0),
    ])
    console_db.flush()
    data = control_center(console_db, Settings(_env_file=None), view="documents", file_id=7)
    assert [row["method"] for row in data["file_pages"]] == ["native", "ocr", "none"]
    assert data["selected_file"]["unread_pages"] == [3]
    assert data["page_records"] == 3 and data["file_total"] == 1
    assert "DO NOT COPY" not in repr(data) and "private-local-path" not in repr(data)
    missing = control_center(console_db, Settings(_env_file=None), view="documents", file_id=999)
    assert missing["file_missing"] and missing["selected_file"] is None


@pytest.mark.parametrize("view", ["overview", "activity", "schedules", "documents", "integrations"])
def test_control_views_render_empty_states(console_db, view):
    data = control_center(console_db, Settings(_env_file=None), view=view)
    env = Environment(loader=FileSystemLoader("src/govcon/web/templates"), autoescape=select_autoescape(["html"]))
    env.globals.update(csrf_token=lambda request: "test", can=lambda user, perm: False)
    env.filters["pt"] = format_pt
    html = env.get_template("operate.html").render(
        request=None, current_user=SimpleNamespace(display_name="Test reviewer", role="reviewer", email="test@example.test"),
        active_page="operate", view=view, console=data,
        overview={"pending_count": 0, "pending": [], "strategy_missing": []},
        integrations=[{"name": "DeepSeek", "status": "Not configured", "tag": "info", "blurb": "Not connected", "rows": []}],
        routing=None,
    )
    assert "Control center" in html and "APIs &amp; models" in html
    assert "private-local-path" not in html
    if view == "integrations":
        assert "No API attempts recorded" in html and "Connection is unverified" in html
    else:
        assert "Refresh every 15s" in html


def test_request_monitor_tracks_errors_and_bounds_history():
    from govcon.web.request_activity import RequestActivity

    monitor = RequestActivity()
    key = monitor.begin("GET", "/workspace/{opp_id}")
    assert monitor.snapshot()["active_count"] == 1
    monitor.finish(key, 500)
    monitor.finish(key, 500)  # duplicate completion cannot count twice
    assert monitor.snapshot()["completed"] == 1 and monitor.snapshot()["server_errors"] == 1
    for _ in range(40):
        key = monitor.begin("GET", "/operate")
        monitor.finish(key, 200)
    snapshot = monitor.snapshot()
    assert snapshot["completed"] == 41 and len(snapshot["recent"]) == 30
    assert snapshot["active_count"] == 0 and snapshot["mean_ms"] is not None


def test_http_telemetry_keeps_route_templates_not_identifiers_or_secrets():
    from fastapi.testclient import TestClient

    from govcon.web.app import create_app

    app = create_app(Settings(_env_file=None))
    client = TestClient(app, follow_redirects=False)
    response = client.get("/workspace/123456?api_key=must-not-be-stored")
    assert response.status_code == 303
    client.get("/unknown-private-identifier?token=must-not-be-stored")
    client.get("/static/govcon.css")
    snapshot = app.state.request_activity.snapshot()
    assert snapshot["completed"] == 2
    assert snapshot["recent"][1]["route"] == "/workspace/{opp_id}"
    assert snapshot["recent"][0]["route"] == "/unmatched"
    assert "must-not-be-stored" not in repr(snapshot) and "123456" not in repr(snapshot)


@pytest.mark.parametrize("view", ["overview", "activity", "schedules", "documents", "integrations", "usage"])
def test_control_views_require_authentication(view):
    from fastapi.testclient import TestClient

    from govcon.web.app import create_app

    client = TestClient(create_app(Settings(_env_file=None)), follow_redirects=False)
    path = "/operate" if view == "overview" else f"/operate/{view}"
    response = client.get(path)
    assert response.status_code == 303 and response.headers["location"] == "/login"


def test_populated_schedules_and_provider_views_escape_errors(console_db, monkeypatch):
    from govcon.operating.console import _run

    now = datetime.now(UTC)
    run = SchedulerJobRun(id=1, chain_name="morning_ingest", trigger="scheduled", status="failed",
                          started_at=now, failed_step="source_documents", steps_completed=["sam_ingest"],
                          error="<script>bad()</script> api_key=private-value")
    console_db.add(run)
    console_db.add(AIProviderCall(id=1, provider="deepseek", model="model", purpose="summary",
                                status="succeeded", created_at=now, latency_ms=240))
    console_db.flush()
    monkeypatch.setattr("govcon.operating.console.chain_board", lambda *args: [{
        "name": "morning_ingest", "description": "Read and match notices", "cron": "6:30 AM PT",
        "steps": ["sam_ingest", "source_documents"], "last_run": run, "last_success": None,
        "last_failure": run, "next_run_local": "Not scheduled", "duration_text": "Not recorded",
        "counts_text": "", "action": "Inspect the failed document step",
    }])
    env = Environment(loader=FileSystemLoader("src/govcon/web/templates"), autoescape=select_autoescape(["html"]))
    env.globals.update(csrf_token=lambda request: "test", can=lambda user, perm: False)
    env.filters["pt"] = format_pt
    data = control_center(console_db, Settings(_env_file=None), view="schedules")
    html = env.get_template("control_center.html").render(console=data, view="schedules")
    assert "1 recorded runs" in html and "source documents" in html
    assert "private-value" not in html and "<script>bad()" not in html
    assert "&lt;script&gt;bad()" in html
    assert _run(run)["failed_step"] == "source_documents"
    providers = control_center(console_db, Settings(_env_file=None), view="integrations")
    html = env.get_template("provider_telemetry.html").render(console=providers)
    assert "bar-succeeded" in html and "240 ms" in html and "Deepseek" in html


@pytest.mark.parametrize("tab", ["summary", "documents"])
def test_workspace_shows_exact_deadline_and_document_trace(tab):
    from govcon.web.routes.workspace import WORKSPACE_MORE_TABS, WORKSPACE_TABS

    env = Environment(loader=FileSystemLoader("src/govcon/web/templates"), autoescape=select_autoescape(["html"]))
    env.globals.update(csrf_token=lambda request: "test", can=lambda user, perm: False)
    env.filters["pt"] = format_pt
    deadline = datetime(2026, 10, 20, 21, 30, tzinfo=UTC)
    opp = SimpleNamespace(id=42, title="Valve notice", source="sam", source_id="NOTICE-42", status="open",
        response_deadline=deadline, psc_code="4810", naics_code="332911", nsn=None, quantity=4, unit="EA",
        agency_path="Test agency", description=None, poc=None)
    file = SimpleNamespace(id=7, filename="Valve specs.pdf", active=True, classification="PUBLIC",
        mime_type="application/pdf", extraction_status="partial", extraction_error=None,
        page_count=3, ocr_pages=[2], ocr_failed_pages=[3], url=None)
    html = env.get_template("workspace.html").render(
        request=None, current_user=SimpleNamespace(display_name="Reviewer", role="reviewer", email="test@example.test"),
        active_page="pipeline", active_tab=tab, opp=opp, pursuit=None, review_session=None,
        bid_decision=None, primary_source_url=None, deadline_class="deadline-warning", deadline_label="10d left",
        workspace_tabs=WORKSPACE_TABS, workspace_more_tabs=WORKSPACE_MORE_TABS,
        progress={"revision": "empty", "active": False}, attachments=[file], can_run_analysis=False,
        ai_summary=None, preparation=None, document_ready=None, stage_trace=[], events=[], contacts=[], source_links=[],
    )
    assert "2026-10-20 14:30 PT" in html
    assert "Proposal &amp; submission" in html
    if tab == "documents":
        assert "/operate/documents?file=7" in html and "1 unreadable" in html
