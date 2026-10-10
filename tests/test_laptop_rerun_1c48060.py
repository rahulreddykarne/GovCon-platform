"""Laptop rerun at 1c48060: download-only retry, lock, budget cache, labels, usage, PT, Pass B."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_web_ui import _make_user
from web_client import CsrfTestClient

from govcon.ai.usage_log import record_call, usage_page
from govcon.config import Settings
from govcon.decision.missing import normalize_missing_information
from govcon.models import (
    AIAnalysis,
    AuditEvent,
    Opportunity,
    OpportunityEvent,
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


def _opp(db, **values) -> Opportunity:
    data = {
        "source": "sam",
        "source_id": f"rerun-{uuid4().hex}",
        "title": f"Rerun {uuid4().hex[:8]}",
        "status": "open",
        "raw": {},
        "links": {},
    }
    data.update(values)
    row = Opportunity(**data)
    db.add(row)
    db.flush()
    return row


def test_rate_limited_download_queues_download_only_task(db, tmp_path) -> None:
    from govcon.enrich.attachments import download_attachments

    opp = _opp(
        db,
        links={"attachments": ["https://api.sam.gov/prod/opportunities/v3/resources/files/a1/download"]},
    )
    settings = Settings(data_dir=tmp_path, sam_api_key="sam-test-key-123456")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "0"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    files = download_attachments(
        db, opp, settings=settings, client=client,
        resolver=lambda _host: ["93.184.216.34"],
    )
    assert files[0].extraction_status == "download_failed"
    download = db.scalar(select(Task).where(Task.opportunity_id == opp.id, Task.task_type == "attachment_download"))
    prep = db.scalar(select(Task).where(Task.opportunity_id == opp.id, Task.task_type == "opportunity_preparation"))
    assert download is not None
    assert prep is None
    assert download.payload.get("retry_reason") == "rate_limited"
    assert download.created_by_user_id is None


def test_download_task_records_document_ready_and_does_not_queue_ai(db) -> None:
    from govcon.tasks.registry import get_handler
    from govcon.workflow.attachment_download import (
        DOCUMENT_READY_EVENT,
        DOCUMENT_READY_MESSAGE,
    )

    handler = get_handler("attachment_download")
    assert [step.name for step in handler.steps] == ["documents"]
    opp = _opp(db)
    db.add(OpportunityEvent(
        opportunity_id=opp.id, event_type=DOCUMENT_READY_EVENT, field_name="attachments",
        new_value={"message": DOCUMENT_READY_MESSAGE, "files": [{"id": 1}]},
    ))
    db.flush()
    from govcon.workflow.attachment_download import document_ready_notice
    notice = document_ready_notice(db, opp.id)
    assert notice is not None
    assert notice["message"] == DOCUMENT_READY_MESSAGE
    later = AIAnalysis(
        opportunity_id=opp.id, analysis_type="solicitation_summary", output_json={"summary": "later"},
    )
    db.add(later)
    db.flush()
    assert later.created_at is not None
    assert document_ready_notice(db, opp.id) is None


def test_waiting_for_budget_is_never_claimed(db) -> None:
    from govcon.tasks import queue

    task, _ = queue.enqueue(db, task_type="ai_analysis", opportunity_id=None,
                            input_revision={"probe": uuid4().hex})
    db.commit()
    settings = Settings(_env_file=None)
    claim = queue.claim(db, worker_id="w", lease_seconds=60, task_id=task.id, settings=settings)
    assert claim is not None
    task = queue.guard_publish(db, claim)
    queue.block(db, task, status="waiting_for_budget", reason="exhausted",
                owner_role="owner", next_action="resume", settings=settings)
    task.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    assert queue.budget_parked(db.get(Task, task.id))
    assert queue.claim(db, worker_id="w2", lease_seconds=60, task_id=task.id, settings=settings) is None
    raised = Settings(_env_file=None, ai_max_input_tokens_per_opportunity=settings.ai_max_input_tokens_per_opportunity * 2)
    assert queue.claim(db, worker_id="w3", lease_seconds=60, task_id=task.id, settings=raised) is None


def test_analysis_lock_rejects_a_second_run(db, monkeypatch) -> None:
    from govcon.enrich.summarize import run_solicitation_analysis
    from govcon.tasks.errors import classify
    from govcon.workflow.analysis_lock import AnalysisInProgress, hold_analysis_lock

    opp = _opp(db)
    db.commit()
    with hold_analysis_lock(db, opp.id):
        with pytest.raises(AnalysisInProgress, match="already running"):
            run_solicitation_analysis(db, opp, settings=Settings(_env_file=None))
    outcome = classify(AnalysisInProgress(opp.id))
    assert outcome.kind == "block" and outcome.status == "waiting_for_input"
    assert "already running" in str(outcome.exc).lower() or "in progress" in outcome.next_action.lower()


def test_part_cache_skips_provider_and_split_parent_does_not_count(db, monkeypatch) -> None:
    from pathlib import Path

    from govcon.ai.budget import (
        AIBudgetExceeded,
        mark_replaced_by_split,
        opportunity_input_used,
        reserve,
    )
    from govcon.ai.structured import run_structured_prompt
    from govcon.prompting.registry import sync_prompts
    from govcon.security.classification import DataClassification

    settings = Settings(_env_file=None, ai_proposal_budget_share=0)
    sync_prompts(db, Path(__file__).parent.parent / "src" / "govcon" / "prompts", settings=settings)
    db.commit()
    opp = _opp(db)
    calls: list[dict] = []

    class Fake:
        name = "deepseek"

        def complete(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                content=(
                    '{"summary":"This cached analysis part has enough substance to be reused.",'
                    '"source_refs":[{"quote":"See page 1 of the RFQ document."}]}'
                ),
                usage={"prompt_tokens": 80, "completion_tokens": 10},
                model="synthetic-model", provider="deepseek", latency_ms=1, finish_reason="stop",
            )

    monkeypatch.setattr("govcon.ai.structured.get_provider", lambda *a, **k: Fake())
    first = run_structured_prompt(
        db, opportunity_id=opp.id, prompt_name="solicitation_analysis",
        analysis_type="solicitation_summary",
        variables={"OPPORTUNITY_JSON": {}, "SOURCE_PACKAGE_JSON": "page one of the rfq"},
        context_manifest={"sha": "abc"}, settings=settings, classification=DataClassification.PUBLIC,
    )
    second = run_structured_prompt(
        db, opportunity_id=opp.id, prompt_name="solicitation_analysis",
        analysis_type="solicitation_summary",
        variables={"OPPORTUNITY_JSON": {}, "SOURCE_PACKAGE_JSON": "page one of the rfq"},
        context_manifest={"sha": "abc"}, settings=settings, classification=DataClassification.PUBLIC,
    )
    assert len(calls) == 1
    assert second.analysis.id == first.analysis.id

    reservation = reserve(
        db, opportunity_id=opp.id, settings=settings, system_prompt="", user_prompt="truncated parent",
        purpose="solicitation_analysis", provider="fake",
    )
    assert reservation is not None
    reservation.finish(SimpleNamespace(usage={"prompt_tokens": 50_000, "completion_tokens": 1}))
    before = opportunity_input_used(db, opp.id)
    mark_replaced_by_split(reservation)
    after = opportunity_input_used(db, opp.id)
    assert after == before - 50_000

    # Limit must accept this request's estimate (257) but not used(80)+requested.
    exhausted = Settings(_env_file=None, ai_max_input_tokens_per_opportunity=300, ai_proposal_budget_share=0)
    with pytest.raises(AIBudgetExceeded, match="used of") as caught:
        reserve(db, opportunity_id=opp.id, settings=exhausted, system_prompt="", user_prompt="x",
                purpose="solicitation_analysis", provider="fake")
    assert caught.value.used == 80 and caught.value.limit == 300
    assert caught.value.spendable == 300


def test_raise_budget_is_audited_and_does_not_auto_resume(db, client) -> None:
    from govcon.tasks import queue

    user, token = _make_user(db, f"budget-{uuid4().hex[:8]}@example.test", "owner")
    client.cookies.set("govcon_session", token)
    opp = _opp(db)
    db.add(Pursuit(opportunity_id=opp.id, stage="evaluating"))
    task, _ = queue.enqueue(db, task_type="ai_analysis", opportunity_id=opp.id,
                            input_revision={"raise": uuid4().hex})
    db.commit()
    claim = queue.claim(db, worker_id="w", lease_seconds=60, task_id=task.id)
    task = queue.guard_publish(db, claim)
    queue.block(db, task, status="waiting_for_budget", reason="exhausted",
                owner_role="owner", next_action="resume")
    db.commit()
    response = client.post(f"/workspace/{opp.id}/raise-budget", data={"new_limit": "240000"})
    assert response.status_code == 303
    db.expire_all()
    assert db.get(Opportunity, opp.id).ai_max_input_tokens == 240000
    parked = db.get(Task, task.id)
    assert parked.status == "waiting_for_budget"
    assert queue.claim(db, worker_id="w2", lease_seconds=60, task_id=task.id) is None
    audit = db.scalar(select(AuditEvent).where(
        AuditEvent.opportunity_id == opp.id, AuditEvent.action_type == "opportunity_budget_raised"))
    assert audit is not None
    assert audit.new_value["limit"] == 240000
    assert audit.new_value["default_unchanged"] == Settings(_env_file=None).ai_max_input_tokens_per_opportunity


def test_missing_information_normalizes_near_duplicates() -> None:
    items = [
        {"field": "Pricing structure", "reason": "missing"},
        {"field": "Pricing structure.format", "reason": "format"},
        {"field": "Items[].quantity", "reason": "qty"},
        {"field": "items.quantity", "reason": "qty2"},
        {"field": "Submission.method", "reason": "portal"},
        {"field": "Clin", "reason": "line"},
        {"field": "nsn", "reason": "stock"},
        {"field": "delivery ('r'/'i')", "reason": "FOB"},
    ]
    assert normalize_missing_information(items) == [
        "Pricing structure",
        "Line items",
        "Submission method",
        "CLIN",
        "NSN",
        "Delivery terms",
    ]


def test_compliance_warnings_are_deduped() -> None:
    from govcon.compliance.pipeline import dedupe_warnings

    warning = {
        "code": "source_ingestion_incomplete",
        "severity": "high",
        "message": "Listed attachments with no extracted content were excluded from this pass and from classification: a.pdf (download_failed).",
    }
    assert dedupe_warnings([warning, dict(warning), {**warning, "severity": "high"}]) == [warning]


def test_usage_page_status_breakdown_and_collapsed_jev(db) -> None:
    from govcon.models import AIProviderCall

    before = {row.id for row in db.scalars(select(AIProviderCall)).all()}

    record_call(db, provider="deepseek", purpose="solicitation_analysis", status="truncated",
                model="t1", usage={"prompt_tokens": 1, "completion_tokens": 1})
    record_call(db, provider="deepseek", purpose="solicitation_analysis", status="output_rejected",
                model="t2", usage={"prompt_tokens": 1, "completion_tokens": 1})
    for _ in range(4):
        record_call(db, provider="jev", purpose="decision_bundle:bid_decision", status="blocked", model="jev-1")
    page = usage_page(Session(db.get_bind()))
    assert page["status_counts"]["truncated"] >= 1
    assert page["status_counts"]["output_rejected"] >= 1
    assert page["status_counts"]["blocked"] >= 4
    truncated = usage_page(Session(db.get_bind()), status="truncated")
    assert all(item["status"] == "truncated" for item in truncated["recent"])
    blocked = usage_page(Session(db.get_bind()), status="blocked")
    jev = [item for item in blocked["recent"] if item.get("grouped_jev")]
    assert jev
    assert any(item.get("count", 1) >= 4 for item in jev)
    leftover = [row for row in Session(db.get_bind()).scalars(select(AIProviderCall)).all() if row.id not in before]
    assert leftover


def test_pass_b_same_model_is_labeled_and_not_independent(db) -> None:
    from govcon.compliance.view import compliance_view

    opp = _opp(db)
    db.flush()
    view = compliance_view(db, opp.id, facts={})
    assert view.pass_b_independent is False
    assert view.pass_b_note == "Pass B: same model, not independent"


def test_decision_timestamp_uses_pt(client, db) -> None:
    from govcon.decision.engine import run_preliminary_decision_package

    user, token = _make_user(db, f"pt-{uuid4().hex[:8]}@example.test", "owner")
    opp = _opp(db)
    run_preliminary_decision_package(db, opportunity_id=opp.id)
    db.commit()
    page = client.get(f"/workspace/{opp.id}?tab=ai_decision", cookies={"govcon_session": token}).text
    assert " PT" in page
    assert "Compliance Risk" not in page


def test_document_ready_banner_renders(client, db) -> None:
    from govcon.workflow.attachment_download import (
        DOCUMENT_READY_EVENT,
        DOCUMENT_READY_MESSAGE,
    )

    user, token = _make_user(db, f"ready-{uuid4().hex[:8]}@example.test", "owner")
    opp = _opp(db)
    db.add(Pursuit(opportunity_id=opp.id, stage="evaluating"))
    db.add(OpportunityEvent(
        opportunity_id=opp.id, event_type=DOCUMENT_READY_EVENT, field_name="attachments",
        new_value={"message": DOCUMENT_READY_MESSAGE, "files": []},
    ))
    db.commit()
    page = client.get(f"/workspace/{opp.id}?tab=overview", cookies={"govcon_session": token}).text
    assert "Document now available, rerun analysis?" in page
    assert f'action="/workspace/{opp.id}/prepare"' in page
