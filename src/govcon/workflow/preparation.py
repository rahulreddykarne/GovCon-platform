"""Automatic opportunity preparation (roadmap gap 4, ADR-067).

Starting a pursuit queues one ``opportunity_preparation`` task. Its steps run
in order, each checkpointed so a retry resumes at the first unfinished step:

1. ``documents``: download every listed attachment, extract pages, OCR pages
   with no text layer, and backfill pages for files extracted earlier.
   Network calls and OCR run with no transaction open.
2. ``summary``: solicitation summary over the whole document set (ADR-066).
3. ``compliance``: the compliance pipeline (requirements, evidence, matrix).
4. ``research``: queue market, supplier and pricing analyses (ADR-064);
   analyses missing their inputs or blocked by policy are listed with the
   action that unblocks them, and never stop preparation.
5. ``decision``: the preliminary decision package.
6. ``review``: open the review session and assign reviewers as the owner's
   Settings page directs.

Steps 2, 3 and 5 make several AI calls through existing services. They run
as recorded passes (``ai/replay.py``): each pass takes the opportunity lock,
then the task lock, runs the service with recorded AI responses, and commits
the service's writes with the step's checkpoint. A provider call with no
recorded response stops the pass, rolls it back and runs with no transaction
open. No transaction stays open during an AI call, and a lost pass repeats
database work, not AI calls.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("govcon.workflow.preparation")

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from govcon.db import session_scope
from govcon.models import FilePage, Opportunity, StoredFile, Task, User
from govcon.tasks import queue
from govcon.tasks.registry import Step, StepContext, TaskHandler, register
from govcon.workflow.source_revision import current_source_revision

PREPARATION_TASK = "opportunity_preparation"
STEPS = ("documents", "summary", "compliance", "research", "decision", "review")
STEP_LABELS = {
    "documents": "Download and read documents",
    "summary": "Solicitation summary",
    "compliance": "Compliance requirements and matrix",
    "research": "Market, supplier and pricing research",
    "decision": "Decision package",
    "review": "Review setup",
}


def queue_preparation(session: Session, *, opportunity_id: int, actor_user_id: int | None) -> tuple[Task, bool]:
    """Queue preparation in the caller's transaction; one active run per source revision."""
    return queue.enqueue(
        session,
        task_type=PREPARATION_TASK,
        opportunity_id=opportunity_id,
        input_revision={"source_revision": str(current_source_revision(session, opportunity_id))},
        actor_user_id=actor_user_id,
    )


def _opportunity(session: Session, task: Task) -> Opportunity:
    opportunity = session.get(Opportunity, task.opportunity_id)
    if opportunity is None:
        raise ValueError(f"opportunity {task.opportunity_id} not found")
    return opportunity


def _note(ctx: StepContext, **data: Any) -> None:
    ctx.checkpoint_data = {**(ctx.checkpoint_data or {}), **data}


# ── 1. documents ──────────────────────────────────────────────────────────────

@dataclass
class _Documents:
    refs: list[Any]
    known: dict[str, set[str]]
    snapshot_id: int | None
    backfill: list[dict[str, Any]] = field(default_factory=list)
    fetched: list[Any] = field(default_factory=list)
    extracted: dict[int, Any] = field(default_factory=dict)


def _documents_prepare(session: Session, task: Task, ctx: StepContext) -> _Documents:
    from govcon.enrich.attachment_refs import attachment_refs_for
    from govcon.enrich.attachments import known_versions, latest_snapshot_id

    opportunity = _opportunity(session, task)
    refs = attachment_refs_for(opportunity)
    with_pages = select(FilePage.file_id).distinct()
    backfill = [
        {"id": row.id, "local_path": row.local_path, "mime": row.mime_type, "filename": row.filename}
        for row in session.scalars(select(StoredFile).where(
            StoredFile.opportunity_id == opportunity.id, StoredFile.active.is_(True),
            StoredFile.local_path.is_not(None), StoredFile.sha256.is_not(None), StoredFile.id.not_in(with_pages),
        ))
    ]
    return _Documents(refs, known_versions(session, opportunity.id, refs),
                      latest_snapshot_id(session, opportunity.id), backfill)


def _documents_execute(docs: _Documents, ctx: StepContext) -> _Documents:
    from govcon.enrich.attachments import fetch_attachment
    from govcon.enrich.extract import extract_text
    from govcon.enrich.ocr import ocr_config
    from govcon.enrich.storage import get_store
    from govcon.http import build_client

    if docs.refs:
        with build_client(ctx.settings, timeout=60.0) as client:
            for ref in docs.refs:
                docs.fetched.append(fetch_attachment(
                    ref, opportunity_id=ctx.opportunity_id, known_shas=docs.known.get(ref.url, set()),
                    settings=ctx.settings, client=client, resolver=None,
                ))
    store = get_store(ctx.settings)
    for item in docs.backfill:
        data = store.read(item["local_path"])
        if data is not None:
            docs.extracted[item["id"]] = extract_text(data, item["mime"] or "", item["filename"], ocr=ocr_config(ctx.settings))
    return docs


def _documents_publish(session: Session, task: Task, docs: _Documents, ctx: StepContext) -> None:
    from govcon.enrich.attachments import reconcile_attachment_versions, record_fetched, write_pages

    opportunity = _opportunity(session, task)
    rows = [record_fetched(session, opportunity, fetched, snapshot_id=docs.snapshot_id) for fetched in docs.fetched]
    reconcile_attachment_versions(session, opportunity, docs.refs,
                                  current={ref.url: row.id for ref, row in zip(docs.refs, rows)})
    for file_id, extraction in docs.extracted.items():
        row = session.get(StoredFile, file_id)
        if row is not None:
            row.extracted_text = extraction.text
            row.extraction_status = extraction.status
            row.extraction_error = extraction.error
            write_pages(session, row, extraction)
    failed = [row.filename or row.url for row in rows if row.extraction_status == "download_failed"]
    ocr_pages = sum(len(row.ocr_pages or []) for row in rows) + sum(len(e.ocr_pages) for e in docs.extracted.values())
    _note(ctx, files=len(rows), backfilled=len(docs.extracted), failed=failed, ocr_pages=ocr_pages)


# ── 2. summary, 3. compliance, 5. decision: existing AI services ─────────────

def _nothing(session: Session, task: Task, ctx: StepContext) -> None:
    ctx.payload["_preparation_claim"] = {
        "task_id": task.id, "task_type": task.task_type, "opportunity_id": task.opportunity_id,
        "token": task.claim_token, "worker_id": task.lease_owner,
    }
    ctx.payload["_preparation_source_revision"] = str(current_source_revision(session, task.opportunity_id))
    return None


def _checkpoint_service(session: Session, ctx: StepContext, step: str, output: dict[str, Any]) -> None:
    from govcon.tasks.errors import TaskSuperseded

    task = queue.guard_publish(session, queue.Claim(**ctx.payload["_preparation_claim"]))
    # now() is fixed at this pass's start; the lease must still hold at commit.
    if task.lease_expires_at <= session.scalar(select(func.clock_timestamp())):
        raise queue.LeaseLost(f"task {ctx.task_id}: lease expired during preparation")
    revision = str(current_source_revision(session, ctx.opportunity_id))
    if revision != ctx.payload["_preparation_source_revision"]:
        raise TaskSuperseded("source changed during preparation", input_revision={"source_revision": revision},
                             payload=dict(task.payload or {}))
    _note(ctx, **output)
    queue.record_step(task, step, ctx.checkpoint_data)


def _run_service(ctx: StepContext, step: str, service: Callable[[Session], dict[str, Any]]) -> dict[str, Any]:
    """Run ``service`` in recorded passes; the last pass commits its writes with the checkpoint."""
    from govcon.ai.replay import run_recorded, stable_ids

    claim = queue.Claim(**ctx.payload["_preparation_claim"])

    def one_pass() -> dict[str, Any]:
        checkpoint_data = ctx.checkpoint_data
        try:
            with session_scope(ctx.settings) as db:
                with stable_ids(db):
                    # Opportunity first, then task (ADR-061), before the service writes anything.
                    queue.guard_publish(db, claim)
                    output = service(db)
                    _checkpoint_service(db, ctx, step, output)
                    db.flush()
                return output
        except BaseException:
            ctx.checkpoint_data = checkpoint_data  # a stopped pass leaves no notes behind
            raise

    def persist(snapshot: dict) -> None:
        try:
            with session_scope(ctx.settings) as db:
                task = db.get(Task, ctx.task_id)
                if task is None or task.status not in {"running", "retrying"}:
                    return
                checkpoint = dict(task.checkpoint or {})
                replays = dict(checkpoint.get("ai_replay") or {})
                replays[step] = snapshot
                checkpoint["ai_replay"] = replays
                task.checkpoint = checkpoint
        except Exception:
            logger.exception("task %s: could not persist the AI replay record for %s", ctx.task_id, step)

    restored = None
    with session_scope(ctx.settings) as db:
        task = db.get(Task, ctx.task_id)
        if task is not None:
            restored = ((task.checkpoint or {}).get("ai_replay") or {}).get(step)
    return run_recorded(one_pass, restore=restored if isinstance(restored, dict) else None, persist=persist)


def _summary_execute(_: None, ctx: StepContext) -> dict[str, Any]:
    from govcon.enrich.summarize import run_solicitation_analysis

    def service(db: Session) -> dict[str, Any]:
        analysis = run_solicitation_analysis(db, db.get(Opportunity, ctx.opportunity_id), settings=ctx.settings)
        if analysis is None:
            return {"analysis_id": None, "note": "No summary: no AI provider configured, the policy blocked the call, "
                                                 "or no document text is available."}
        gaps = (analysis.context_manifest or {}).get("coverage", {}).get("gaps") or []
        return {"analysis_id": analysis.id, "gaps": gaps}

    return _run_service(ctx, "summary", service)


def _compliance_execute(_: None, ctx: StepContext) -> dict[str, Any]:
    from govcon.ai.providers import provider_available
    from govcon.compliance.pipeline import run_compliance_pipeline

    use_ai = provider_available(ctx.settings)
    now = datetime.now(UTC)  # one clock for every pass, so passes build the same prompts

    def service(db: Session) -> dict[str, Any]:
        result = run_compliance_pipeline(db, ctx.opportunity_id, use_ai=use_ai, settings=ctx.settings, now=now)
        return {"status": result["status"], "matrix_run_id": result.get("matrix_run_id")}

    return _run_service(ctx, "compliance", service)


def _decision_execute(_: None, ctx: StepContext) -> dict[str, Any]:
    from govcon.decision.engine import run_preliminary_decision_package

    def service(db: Session) -> dict[str, Any]:
        package = run_preliminary_decision_package(db, opportunity_id=ctx.opportunity_id, settings=ctx.settings)
        return {"analysis_id": package.analysis.id, "recommendation": package.bid_decision.recommendation}

    return _run_service(ctx, "decision", service)


def _record(session: Session, task: Task, output: dict[str, Any], ctx: StepContext) -> None:
    _note(ctx, **output)


# ── 4. research ───────────────────────────────────────────────────────────────

def _research_publish(session: Session, task: Task, _: None, ctx: StepContext) -> None:
    from govcon.ai.structured import StructuredCallError
    from govcon.intelligence.ai_analyses import ANALYSIS_KINDS, AnalysisInputMissing
    from govcon.intelligence.analysis_tasks import queue_analysis

    outcomes: dict[str, str] = {}
    for kind in ANALYSIS_KINDS:
        try:
            analysis_task, _created = queue_analysis(session, opportunity_id=task.opportunity_id, kind=kind,
                                                     actor_user_id=task.created_by_user_id, settings=ctx.settings)
            outcomes[kind] = f"queued as task {analysis_task.id}"
        except AnalysisInputMissing as exc:
            outcomes[kind] = f"waiting for input: {exc}"
        except StructuredCallError as exc:
            outcomes[kind] = f"not run ({exc.reason}): {exc.detail}"
    _note(ctx, analyses=outcomes)


# ── 6. review ─────────────────────────────────────────────────────────────────

def _review_publish(session: Session, task: Task, _: None, ctx: StepContext) -> None:
    from govcon.collaboration.assignments import assign_reviewer, assignment_for_user
    from govcon.collaboration.review_sessions import ensure_review_session
    from govcon.workflow.app_settings import REVIEWER_ASSIGNMENT, get_setting

    review = ensure_review_session(session, opportunity_id=task.opportunity_id)
    policy = get_setting(session, REVIEWER_ASSIGNMENT)
    if review.status not in ("ready_for_review", "under_review", "pending"):
        _note(ctx, reviewers=[], note=f"review is already {review.status}; no assignment made")
        return
    if policy["mode"] == "named":
        user_ids = list(policy["user_ids"])
    elif policy["mode"] == "all_active":
        user_ids = list(session.scalars(select(User.id).where(User.is_active.is_(True), User.role == "reviewer")))
    else:
        _note(ctx, reviewers=[], note="Reviewer assignment is manual: assign reviewers on the Review tab.")
        return
    assigned = []
    for user_id in user_ids:
        user = session.get(User, user_id)
        if user is None or not user.is_active:
            continue
        existing = assignment_for_user(session, opportunity_id=task.opportunity_id, user_id=user_id)
        if existing is None or existing.status not in {"assigned", "in_progress", "complete", "reopened"}:
            assign_reviewer(session, opportunity_id=task.opportunity_id, user_id=user_id,
                            actor_user_id=task.created_by_user_id)
        assigned.append(user_id)
    note = None if assigned else "No active reviewers matched the Settings page; assign reviewers on the Review tab."
    _note(ctx, reviewers=assigned, note=note)


register(TaskHandler(task_type=PREPARATION_TASK, steps=[
    Step("documents", prepare=_documents_prepare, execute=_documents_execute, publish=_documents_publish,
         timeout_seconds=3600),
    Step("summary", prepare=_nothing, execute=_summary_execute, publish=_record, timeout_seconds=3600),
    Step("compliance", prepare=_nothing, execute=_compliance_execute, publish=_record, timeout_seconds=3600),
    Step("research", prepare=_nothing, publish=_research_publish, timeout_seconds=300),
    Step("decision", prepare=_nothing, execute=_decision_execute, publish=_record, timeout_seconds=1800),
    Step("review", prepare=_nothing, publish=_review_publish, timeout_seconds=300),
]))


def preparation_view(task: Task | None) -> dict[str, Any] | None:
    """The workspace panel's view: each step's state and recorded notes."""
    if task is None:
        return None
    checkpoint = task.checkpoint or {}
    done = set(checkpoint.get("completed_steps") or [])
    data = checkpoint.get("data") or {}
    steps = []
    for name in STEPS:
        if name in done:
            state = "done"
        elif task.status in ("running", "retrying") and task.current_step == name:
            state = task.status
        elif task.status in ("failed", "waiting_for_input", "waiting_for_budget") and task.current_step == name:
            state = task.status
        else:
            state = "pending"
        steps.append({"name": name, "label": STEP_LABELS[name], "state": state, "data": data.get(name) or {}})
    return {"task": task, "steps": steps, "active": task.status in ("queued", "running", "retrying")}
