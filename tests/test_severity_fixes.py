"""Regression coverage for the October production audit, in severity order."""

from __future__ import annotations

import os
import socket
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest

from govcon.bots import store
from govcon.config import Settings


@pytest.mark.skipif(os.name != "nt", reason="native Windows process-status check")
def test_windows_live_owner_is_checked_without_sending_signals(monkeypatch):
    signal = Mock()
    monkeypatch.setattr(store.os, "kill", signal)
    owner = f"{socket.gethostname()}:{os.getpid()}:audit"
    assert store.owner_still_running(owner, {owner: datetime.now(UTC)})
    signal.assert_not_called()


@pytest.mark.parametrize("provider", ["anthropic", "openai", "deepseek"])
def test_default_route_uses_the_selected_providers_default(provider):
    from govcon.ai.providers import resolve_provider_model
    from govcon.ai.routing import analysis_selection

    settings = Settings(_env_file=None, ai_primary_provider=provider, deepseek_model="other-provider-model",
                        anthropic_model=None, openai_model=None,
                        **{f"{provider}_api_key": "synthetic-test-key"})
    chosen_provider, chosen_model, _ = analysis_selection(None, settings, provider_name=None, model=None)
    assert (chosen_provider, chosen_model) == resolve_provider_model(settings, provider_name=provider)


def test_stale_or_invalid_owner_never_probes_a_process(monkeypatch):
    signal = Mock()
    monkeypatch.setattr(store.os, "kill", signal)
    stale = f"{socket.gethostname()}:{os.getpid()}:stale"
    assert not store.owner_still_running(stale, {stale: datetime.now(UTC) - timedelta(hours=1)})
    for pid in ("0", "-1", "invalid"):
        owner = f"{socket.gethostname()}:{pid}:invalid"
        assert not store.owner_still_running(owner, {owner: datetime.now(UTC)})
    signal.assert_not_called()


def test_document_backlog_reaches_old_notice_after_200_retained_files(db, monkeypatch):
    from govcon.enrich.attachment_refs import attachment_refs_for
    from govcon.models import Opportunity, StoredFile
    from govcon.scheduler.jobs import step_source_documents

    notices = [Opportunity(source="sam", source_id=f"backlog-{uuid4().hex}", status="open", raw={},
                           links={"description": f"https://example.test/{uuid4().hex}"},
                           updated_at=datetime.now(UTC) - timedelta(days=2)) for _ in range(201)]
    db.add_all(notices)
    db.flush()
    for notice in notices[:200]:
        db.add(StoredFile(opportunity_id=notice.id, url=attachment_refs_for(notice)[0].url,
                          sha256="a" * 64, active=True, classification="PUBLIC", source_origin="test",
                          extraction_status="success"))
    db.flush()
    downloaded = []

    def download(session, opportunity, settings=None):
        downloaded.append(opportunity.id)
        row = StoredFile(opportunity_id=opportunity.id, url=attachment_refs_for(opportunity)[0].url,
                         sha256="b" * 64, active=True, classification="PUBLIC", source_origin="test",
                         extraction_status="success")
        session.add(row)
        session.flush()
        return [row]

    monkeypatch.setattr("govcon.enrich.attachments.download_attachments", download)
    result = step_source_documents(db, Settings(_env_file=None))
    assert result.status == "succeeded"
    assert notices[-1].id in downloaded
    db.rollback()


def test_dibbs_catchup_keeps_the_oldest_pending_indexes():
    from datetime import date

    from govcon.ingest.dibbs import ARCHIVE_ROOT, _catchup_urls

    links = [f"{ARCHIVE_ROOT}/in2609{day:02}.txt" for day in range(2, 22)]
    assert _catchup_urls(links, date(2026, 9, 1)) == links[:14]


def test_dibbs_retries_a_failed_day_below_the_newest_watermark(db, tmp_path):
    from pathlib import Path

    import httpx

    from govcon.ingest.dibbs import ARCHIVE_ROOT, pull_dibbs_index
    from govcon.models import IngestionRun

    missed = f"{ARCHIVE_ROOT}/in260924.txt"
    db.add(IngestionRun(job="dibbs_index", started_at=datetime.now(UTC), status="completed_with_errors",
                        errors={"messages": [f"DIBBS index file was not available at {missed}"],
                                "details": {"indexes": ["in260925.txt"]}}))
    db.flush()
    seen = []
    payload = Path("tests/fixtures/dibbs/in260925.txt").read_bytes()

    def response(request):
        url = str(request.url)
        seen.append(url)
        if "RFQDates.aspx" in url:
            return httpx.Response(200, text=f"<a href='{missed}'>missed</a><a href='{ARCHIVE_ROOT}/in260925.txt'>newest</a>")
        return httpx.Response(200, content=payload)

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        pull_dibbs_index(db, settings=Settings(_env_file=None, data_dir=tmp_path), client=client, interval=0)
    assert missed in seen
    db.rollback()


def test_cancelled_bot_outputs_are_rolled_back_before_publication(upgraded_engine, monkeypatch):
    from types import SimpleNamespace

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from govcon.config import get_settings
    from govcon.db import session_scope
    from govcon.models import BotRun, Task
    from govcon.tasks import queue
    from govcon.tasks.handlers.bots import run_bot_task

    settings = get_settings()
    with session_scope(settings) as session:
        task, _ = queue.enqueue(session, task_type="bot_run", opportunity_id=None,
                                input_revision={"cancel-probe": uuid4().hex},
                                payload={"bot_name": "orchestrator", "slot": uuid4().hex, "pull": False})
        task_id = task.id
    with session_scope(settings) as session:
        claim = queue.claim(session, worker_id="test:1:owner", lease_seconds=300, task_id=task_id)
    assert claim is not None
    key = uuid4().hex

    def orchestrate(session, settings, **kwargs):
        # Model the visible-start commit, followed by business writes while
        # another transaction cancels this claim.
        session.commit()
        with session_scope(settings) as other:
            queue.cancel(other, other.get(Task, task_id), reason="operator cancelled")
        run = BotRun(bot_name="orchestrator", status="succeeded", trigger="test",
                     idempotency_key=key, started_at=datetime.now(UTC), attempt=1, inputs={}, outputs={})
        session.add(run)
        session.flush()
        return run

    monkeypatch.setattr("govcon.bots.orchestrator.execute_orchestrator", orchestrate)
    with pytest.raises(queue.LeaseLost):
        run_bot_task(settings, claim, SimpleNamespace(lost=False, set_deadline=Mock()))
    with Session(upgraded_engine) as session:
        assert session.scalar(select(BotRun.id).where(BotRun.idempotency_key == key)) is None
        assert session.get(Task, task_id).status == "cancelled"


def test_publication_rejects_a_lease_expired_during_the_transaction(db):
    import time

    from govcon.tasks import queue

    task, _ = queue.enqueue(db, task_type="bot_run", opportunity_id=None,
                            input_revision={"expired": uuid4().hex})
    task.status = "running"
    task.lease_owner = "test-owner"
    task.claim_token = 1
    task.lease_expires_at = datetime.now(UTC) + timedelta(milliseconds=50)
    db.flush()
    claim = queue.Claim(task.id, "bot_run", None, 1, "test-owner")
    time.sleep(0.1)
    with pytest.raises(queue.LeaseLost):
        queue.guard_publish(db, claim)
    db.rollback()


def test_orchestrator_resumes_a_fresh_run_from_an_older_task_claim(db, monkeypatch):
    from govcon.bots.orchestrator import execute_orchestrator
    from govcon.models import BotRun
    from govcon.tasks import queue

    slot = uuid4().hex
    task, _ = queue.enqueue(db, task_type="bot_run", opportunity_id=None, input_revision={"resume": slot})
    task.claim_token = 1
    db.flush()
    claim = queue.claim(db, task_id=task.id, worker_id="new-worker", lease_seconds=300)
    assert claim is not None
    old = BotRun(bot_name="orchestrator", status="running", trigger="worker",
                 idempotency_key=f"orchestrator:{slot}:pull=0", started_at=datetime.now(UTC),
                 inputs={"slot": slot, "pull": False, "task_id": task.id, "claim_token": 1}, attempt=1)
    db.add(old)
    db.flush()
    discovery = Mock(return_value=type("Run", (), {"id": 2, "bot_name": "discovery", "status": "succeeded",
                                                  "opportunity_id": None, "attempt": 1,
                                                  "outputs": {"opportunity_ids": []}})())
    monkeypatch.setattr("govcon.bots.orchestrator.execute_discovery", discovery)
    monkeypatch.setattr("govcon.bots.orchestrator.execute_alert", discovery)
    monkeypatch.setattr("govcon.bots.orchestrator.execute_operations", discovery)
    resumed = execute_orchestrator(db, Settings(_env_file=None), trigger="worker", slot=slot, pull=False,
                                   task_claim=claim)
    assert resumed.id == old.id and resumed.attempt == 2 and resumed.status == "succeeded"
    assert discovery.call_count == 3
    db.rollback()


def test_matching_cache_changes_with_watchlist_criteria(db):
    from govcon.bots.workflows import execute_matching
    from govcon.models import Opportunity, Watchlist

    opp = Opportunity(source="demo", source_id=uuid4().hex, raw={}, status="open", psc_code="TEST")
    watch = Watchlist(name=uuid4().hex, enabled=True, psc_codes=["WRONG"])
    db.add_all([opp, watch])
    db.flush()
    settings = Settings(_env_file=None)
    first = execute_matching(db, settings, opportunity_id=opp.id, trigger="test", parent_run_id=None)
    watch.psc_codes = ["TEST"]
    db.flush()
    second = execute_matching(db, settings, opportunity_id=opp.id, trigger="test", parent_run_id=None)
    assert second.id != first.id
    assert second.outputs["matched"] is True
    assert execute_matching(db, settings, opportunity_id=opp.id, trigger="test", parent_run_id=None).id == second.id
    db.rollback()


def test_awards_cache_changes_when_history_arrives(db):
    from govcon.bots.workflows import execute_awards
    from govcon.models import Award, Opportunity

    opp = Opportunity(source="demo", source_id=uuid4().hex, raw={}, status="open", psc_code="TEST")
    db.add(opp)
    db.flush()
    settings = Settings(_env_file=None)
    first = execute_awards(db, settings, opportunity_id=opp.id, trigger="test", parent_run_id=None)
    db.add(Award(award_id=uuid4().hex, psc_code="TEST", raw={}, total_obligation=100))
    db.flush()
    second = execute_awards(db, settings, opportunity_id=opp.id, trigger="test", parent_run_id=None)
    assert second.id != first.id and len(second.outputs["comps"]) == 1
    db.rollback()


@pytest.mark.parametrize("path", ["/suppliers", "/workspace/1/quotes", "/login"])
def test_request_stream_is_limited_before_form_parsing(path, monkeypatch):
    import asyncio

    from govcon.web import security

    monkeypatch.setattr(security, "FORM_BODY_LIMIT", 100, raising=False)
    monkeypatch.setattr(security, "SOURCING_UPLOAD_LIMIT", 100, raising=False)
    called = []
    sent = []

    async def inner(*args):
        called.append(True)

    async def receive():
        return {"type": "http.request", "body": b"x" * 101, "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(security.PackageUploadLimitMiddleware(inner)(
        {"type": "http", "path": path, "method": "POST", "headers": []}, receive, send))
    assert not called
    assert sent[0]["status"] == 413


@pytest.mark.parametrize("outcome", ["failed", "completed_with_errors"])
def test_failed_bot_is_not_published_as_task_success(upgraded_engine, monkeypatch, outcome):
    from types import SimpleNamespace

    from govcon.config import get_settings
    from govcon.db import session_scope
    from govcon.models import Task
    from govcon.tasks import queue
    from govcon.tasks.handlers.bots import run_bot_task

    settings = get_settings()
    with session_scope(settings) as db:
        task, _ = queue.enqueue(db, task_type="bot_run", opportunity_id=None, input_revision={"failure": uuid4().hex},
                                payload={"bot_name": "orchestrator", "slot": uuid4().hex, "pull": False})
        task_id = task.id
    with session_scope(settings) as db:
        claim = queue.claim(db, worker_id="test", lease_seconds=300, task_id=task_id)
    monkeypatch.setattr("govcon.bots.orchestrator.execute_orchestrator",
                        lambda *a, **k: SimpleNamespace(id=999, status=outcome, outputs={}, error="test failure"))
    assert run_bot_task(settings, claim, SimpleNamespace(lost=False, set_deadline=Mock())) == "retrying"
    with session_scope(settings) as db:
        assert db.get(Task, task_id).status == "retrying"


@pytest.mark.parametrize("already_complete", [False, True])
def test_return_for_ai_queues_fresh_preparation_once(db, monkeypatch, already_complete):
    from sqlalchemy import select
    from test_collaborative_review import _opp, _user

    from govcon.collaboration.assignments import assign_reviewer
    from govcon.collaboration.review_sessions import (
        complete_assignment,
        ensure_review_session,
    )
    from govcon.models import Task

    opp = _opp(db)
    reviewer = _user(db, "reviewer", "return-ai")
    owner = _user(db, "owner", "return-ai")
    other = _user(db, "reviewer", "other-return-ai")
    assignment = assign_reviewer(db, opportunity_id=opp.id, user_id=reviewer.id, actor_user_id=owner.id)
    assign_reviewer(db, opportunity_id=opp.id, user_id=other.id, actor_user_id=owner.id)
    review = ensure_review_session(db, opportunity_id=opp.id, review_policy="single")
    monkeypatch.setattr("govcon.collaboration.review_sessions.run_consolidated_review_prompt",
                        Mock(side_effect=RuntimeError("offline")))
    complete_assignment(db, opportunity_id=opp.id, user_id=other.id, action="approve_continue", agree_with_ai_assessment=True)
    if already_complete:
        complete_assignment(db, opportunity_id=opp.id, user_id=reviewer.id, action="approve_continue", agree_with_ai_assessment=True)
    for _ in range(2):
        complete_assignment(db, opportunity_id=opp.id, user_id=reviewer.id, action="return_for_ai_analysis")
        assert review.status == "returned_for_review" and review.completed_review_count == 0
        # Loading the workspace starts a reopened assignment. A duplicate
        # request must still reuse the preparation already in progress.
        assignment.status = "in_progress"
        db.flush()
    tasks = list(db.scalars(select(Task).where(Task.opportunity_id == opp.id,
                                               Task.task_type == "opportunity_preparation")))
    assert len(tasks) == 1
    assert tasks[0].status == "queued" and tasks[0].input_revision["review_return_version"]
    db.rollback()


def test_comment_changes_and_async_opinions_invalidate_consolidation(db, monkeypatch):
    from test_collaborative_review import _opp, _user

    from govcon.collaboration.assignments import assign_reviewer
    from govcon.collaboration.comments import add_comment
    from govcon.collaboration.review_sessions import (
        complete_assignment,
        ensure_review_session,
        recalculate_quorum,
    )

    opp = _opp(db)
    reviewer = _user(db, "reviewer", "summary-refresh")
    assign_reviewer(db, opportunity_id=opp.id, user_id=reviewer.id)
    review = ensure_review_session(db, opportunity_id=opp.id, review_policy="single")
    monkeypatch.setattr("govcon.collaboration.review_sessions.run_consolidated_review_prompt", Mock(side_effect=RuntimeError("offline")))
    complete_assignment(db, opportunity_id=opp.id, user_id=reviewer.id, action="approve_continue", agree_with_ai_assessment=True)
    before = dict(review.ai_consolidated_review)
    version = review.version
    comment = add_comment(db, opportunity_id=opp.id, user_id=reviewer.id,
                          body="A newly discovered delivery risk needs another look.", topic="delivery", validate_with_ai=False)
    recalculate_quorum(db, opportunity_id=opp.id)
    assert review.ai_consolidated_review["quorum_signature"] != before["quorum_signature"]
    assert review.version > version
    comment.ai_position = "disagree"
    comment.ai_missing_information = ["Verify the changed lead time"]
    db.flush()
    recalculate_quorum(db, opportunity_id=opp.id)
    assert "delivery" in review.ai_consolidated_review["new_material_risks"]
    assert "Verify the changed lead time" in review.ai_consolidated_review["open_questions"]
    db.rollback()


def test_semantic_search_excludes_other_models_and_stale_text(db):
    from govcon.enrich.embeddings import _source_hash, opportunity_text
    from govcon.matching.semantic import similar_opportunities
    from govcon.models import Opportunity

    vector = [1.0] + [0.0] * 383
    rows = [Opportunity(source="demo", source_id=uuid4().hex, raw={}, title=f"model notice {i}",
                        embedding=vector, embedding_model=model, embedding_dimension=384)
            for i, model in enumerate(["new@v2", "old@v1", "new@v2", "new@v2"])]
    for row in rows:
        row.embedding_source_hash = _source_hash(opportunity_text(row))
    db.add_all(rows)
    db.flush()
    rows[2].title = "Changed after embedding"
    db.flush()
    result = similar_opportunities(db, rows[0].id)
    assert result["method"] == "vector"
    assert {match["id"] for match in result["matches"]} == {rows[3].id}
    db.rollback()


@pytest.mark.parametrize("step", ["summary", "compliance"])
def test_returned_review_bypasses_the_analysis_cache(db, monkeypatch, step):
    from types import SimpleNamespace

    from test_collaborative_review import _opp

    from govcon.tasks.registry import StepContext
    from govcon.workflow import preparation

    opp = _opp(db)
    ctx = StepContext(Settings(_env_file=None), 1, opp.id, {}, {"review_return_version": 2})
    monkeypatch.setattr(preparation, "_run_service", lambda ctx, step, service: service(db))
    run = Mock(return_value=SimpleNamespace(id=1, context_manifest={}) if step == "summary" else {"status": "succeeded"})
    monkeypatch.setattr("govcon.enrich.summarize.run_solicitation_analysis" if step == "summary" else
                        "govcon.compliance.pipeline.run_compliance_pipeline", run)
    getattr(preparation, f"_{step}_execute")(None, ctx)
    assert run.call_args.kwargs.get("force") is True
    db.rollback()


def test_bid_cache_changes_with_commercial_facts(db, monkeypatch):
    from types import SimpleNamespace

    from test_collaborative_review import _opp

    from govcon.bots.workflows import execute_bid
    from govcon.models import Pursuit

    opp = _opp(db)
    pursuit = Pursuit(opportunity_id=opp.id, stage="evaluating", sourcing_cost=10, quote_price=20)
    db.add(pursuit)
    db.flush()
    package = SimpleNamespace(bundle_runs=[], bid_decision=SimpleNamespace(recommendation="needs_more_info", missing_information={}))
    decide = Mock(return_value=package)
    monkeypatch.setattr("govcon.decision.engine.run_preliminary_decision_package", decide)
    settings = Settings(_env_file=None)
    first = execute_bid(db, settings, opportunity_id=opp.id, trigger="test", parent_run_id=None)
    pursuit.quote_price = 30
    db.flush()
    second = execute_bid(db, settings, opportunity_id=opp.id, trigger="test", parent_run_id=None)
    assert first.id != second.id
    assert execute_bid(db, settings, opportunity_id=opp.id, trigger="test", parent_run_id=None).id == second.id
    assert decide.call_count == 2
    db.rollback()


def test_catalog_limit_rejects_before_database_work(monkeypatch):
    from types import SimpleNamespace

    from govcon.sourcing import records

    db = Mock()
    monkeypatch.setattr(records, "MAX_CATALOG_BYTES", 100)
    with pytest.raises(records.SourcingError, match="limited"):
        records.import_catalog_csv(db, supplier_id=1, data=b"x" * 101, filename="catalog.csv",
                                   actor=SimpleNamespace(is_active=True, role="owner"))
    db.get.assert_not_called()


def test_catalog_row_limit_rejects_before_any_product_write(monkeypatch):
    from types import SimpleNamespace

    from govcon.sourcing import records

    db = Mock()
    monkeypatch.setattr(records, "MAX_CATALOG_ROWS", 1)
    with pytest.raises(records.SourcingError, match="limited"):
        records.import_catalog_csv(db, supplier_id=1, data=b"part_number\nA\nB\n", filename="catalog.csv",
                                   actor=SimpleNamespace(is_active=True, role="owner"))
    db.add.assert_not_called()


def test_failed_dibbs_retries_do_not_block_new_indexes(db, tmp_path, monkeypatch):
    from pathlib import Path

    import httpx

    from govcon.ingest import dibbs
    from govcon.ingest.runs import IngestStats
    from govcon.models import IngestionRun

    failed = [f"{dibbs.ARCHIVE_ROOT}/in2609{day:02}.txt" for day in range(1, 15)]
    fresh = f"{dibbs.ARCHIVE_ROOT}/in260916.txt"
    db.add(IngestionRun(job="dibbs_index", started_at=datetime.now(UTC), status="completed_with_errors",
                        errors={"messages": [f"download failed: {url}" for url in failed],
                                "details": {"indexes": ["in260915.txt"]}}))
    db.flush()
    seen = []

    def pull(session, client, url, **kwargs):
        seen.append(url)
        if url in failed:
            raise dibbs.DibbsError(f"download failed: {url}")
        return dibbs.DibbsIngestResult(IngestStats(fetched=1), dibbs.DibbsCoverage(1, 0, 0), Path(url).name)

    monkeypatch.setattr(dibbs, "_pull_one", pull)
    listing = "".join(f"<a href='{url}'>index</a>" for url in [*failed, fresh])
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=listing))) as client:
        result = dibbs.pull_dibbs_index(db, settings=Settings(_env_file=None, data_dir=tmp_path), client=client, interval=0)
    assert fresh in seen and result.index_name == "in260916.txt"
    assert len(seen) <= dibbs.MAX_CATCHUP_INDEXES
    db.add(IngestionRun(job="dibbs_index", started_at=datetime.now(UTC), status="completed_with_errors",
                        errors={"messages": result.stats.errors, "details": result.run_details()}))
    db.flush()
    assert dibbs._pending_index_urls(db)[0] == failed[-1], "unattempted failures must get the next retry slot"
    db.rollback()


def test_document_scan_advances_past_a_full_batch_of_failures(db, monkeypatch):
    from govcon.models import Opportunity
    from govcon.scheduler import jobs

    notices = [Opportunity(source="sam", source_id=uuid4().hex, raw={},
                           links={"description": f"https://example.test/{uuid4().hex}"}) for _ in range(41)]
    db.add_all(notices)
    db.flush()
    seen = []

    def download(session, opportunity, settings):
        seen.append(opportunity.id)
        if opportunity.id != notices[-1].id:
            raise RuntimeError("temporary download failure")
        return []

    monkeypatch.setattr("govcon.enrich.attachments.download_attachments", download)
    monkeypatch.setattr(jobs, "_source_document_page", lambda session, after, upper:
                        [notice for notice in notices if after < notice.id <= upper])
    first = jobs.step_source_documents(db, Settings(_env_file=None))
    assert first.status == "completed_with_errors" and len(seen) == 40
    second = jobs.step_source_documents(db, Settings(_env_file=None))
    assert second.status == "succeeded" and seen[-1] == notices[-1].id
    db.rollback()


def test_matching_cache_refreshes_at_a_deadline_threshold(db, monkeypatch):
    from govcon.bots import workflows
    from govcon.models import Opportunity, Watchlist

    moment = [datetime(2026, 10, 10, 12, tzinfo=UTC)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment[0]

    watch = Watchlist(name=uuid4().hex, enabled=True, psc_codes=["EDGE"], min_deadline_days=1)
    opp = Opportunity(source="demo", source_id=uuid4().hex, raw={}, status="open", psc_code="EDGE",
                      response_deadline=moment[0] + timedelta(days=1, seconds=1))
    db.add_all([watch, opp])
    db.flush()
    monkeypatch.setattr(workflows, "datetime", Clock)
    first = workflows.execute_matching(db, Settings(_env_file=None), opportunity_id=opp.id, trigger="test", parent_run_id=None)
    moment[0] += timedelta(seconds=2)
    second = workflows.execute_matching(db, Settings(_env_file=None), opportunity_id=opp.id, trigger="test", parent_run_id=None)
    assert second.id != first.id
    groups = {group["watchlist_id"]: group for group in second.outputs["watchlists"]}
    assert not groups[watch.id]["matched"]
    db.rollback()
