"""Operations endpoints and supporting views."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import desc, func, select, text

from govcon.collaboration.users import can
from govcon.db import session_scope
from govcon.learning.analytics import WIN_PROFILE_MINIMUM, outcome_analytics
from govcon.models import IngestionRun, Match, Opportunity, SchedulerJobRun, Task, User
from govcon.tasks.queue import cancel as cancel_task
from govcon.tasks.queue import requeue
from govcon.web.presentation import ViewRow
from govcon.web.routes.common import (
    _WORKFLOW_ERRORS,
    _actor,
    _error_text,
    _NeedsLogin,
    _redirect,
    _render,
    _require_login,
)
from govcon.workflow.invalidation import lock_one


def ops(request: Request) -> Response:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    with session_scope() as db:
        opp_count = db.scalar(select(func.count()).select_from(Opportunity)) or 0
        opp_open = db.scalar(select(func.count()).select_from(Opportunity).where(Opportunity.status == "open")) or 0
        match_count = db.scalar(select(func.count()).select_from(Match).where(Match.active.is_(True))) or 0
        match_new = db.scalar(
            select(func.count()).select_from(Match).where(Match.status == "new", Match.active.is_(True))
        ) or 0
        match_pursuing = db.scalar(select(func.count()).select_from(Match).where(Match.status == "pursuing")) or 0
        user_count = db.scalar(select(func.count()).select_from(User).where(User.is_active == True)) or 0

        runs = db.scalars(
            select(IngestionRun).order_by(desc(IngestionRun.started_at)).limit(30)
        ).all()

        # Scheduler job runs — latest per chain + last 20 overall
        job_runs = db.scalars(
            select(SchedulerJobRun).order_by(desc(SchedulerJobRun.started_at)).limit(50)
        ).all()

        from govcon.ops.health import chain_board, collect_health, record_heartbeat

        record_heartbeat(db, role="web", instance_id="web")
        health = collect_health(db, request.app.state.settings)
        chain_summary = chain_board(db)

        users = db.scalars(select(User).order_by(User.email)).all() if can(user, "manage_users") else []

        # Durable tasks (ADR-061): counts, terminal failures and blocked work.
        task_counts = dict(db.execute(select(Task.status, func.count()).group_by(Task.status)).all())
        failed_tasks = db.scalars(
            select(Task).where(Task.status == "failed").order_by(desc(Task.finished_at), desc(Task.id)).limit(50)
        ).all()
        waiting_tasks = db.scalars(
            select(Task).where(Task.status.in_(("waiting_for_input", "waiting_for_budget")))
            .order_by(Task.id).limit(50)
        ).all()
        oldest_queued = db.scalar(select(func.min(Task.created_at)).where(Task.status == "queued"))

        # Measured AI usage (roadmap §6.2): settled tokens and recorded cost, last 30 days.
        from govcon.models import AICallUsage

        day = func.date_trunc("day", AICallUsage.created_at)
        ai_usage = db.execute(
            select(day.label("day"), AICallUsage.purpose, AICallUsage.model,
                   func.count().label("calls"),
                   func.sum(AICallUsage.input_tokens).label("input_tokens"),
                   func.sum(AICallUsage.output_tokens).label("output_tokens"),
                   func.sum(AICallUsage.cost_usd).label("cost_usd"),
                   func.count().filter(AICallUsage.status == "failed").label("failed"))
            .where(AICallUsage.created_at >= func.now() - text("interval '30 days'"))
            .group_by(day, AICallUsage.purpose, AICallUsage.model)
            .order_by(day.desc(), AICallUsage.purpose)
            .limit(200)
        ).all()

    stats = ViewRow({
        "opp_count": opp_count,
        "opp_open": opp_open,
        "match_count": match_count,
        "match_new": match_new,
        "match_pursuing": match_pursuing,
        "user_count": user_count,
    })

    return _render(request, "ops.html", {
        "stats": stats,
        "runs": list(runs),
        "job_runs": list(job_runs),
        "chain_summary": chain_summary,
        "health": health,
        "users": list(users),
        "task_counts": task_counts,
        "failed_tasks": list(failed_tasks),
        "waiting_tasks": list(waiting_tasks),
        "oldest_queued": oldest_queued,
        "ai_usage": list(ai_usage),
        "can_manage_tasks": can(user, "approve"),
        "active_page": "ops",
    }, user)


def ops_task_action(request: Request, task_id: int, action: str) -> Response:
    """Re-queue a failed or waiting task, or cancel an active one (owner/approver)."""
    from govcon.tasks.queue import ACTIVE_STATUSES

    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    if action not in ("retry", "cancel"):
        return HTMLResponse("Unknown action", status_code=404)
    try:
        with session_scope() as db:
            actor = _actor(db, user, "approve")
            task = lock_one(db, select(Task).where(Task.id == task_id))
            if task is None:
                raise ValueError(f"task {task_id} not found")
            if action == "retry":
                requeue(db, task, actor_user_id=actor.id, reason="retried from /ops")
            else:
                if task.status not in ACTIVE_STATUSES:
                    raise ValueError(f"task {task_id} is {task.status}; only active tasks can be cancelled")
                cancel_task(db, task, reason=f"cancelled from /ops by {actor.email}", actor_user_id=actor.id)
    except _WORKFLOW_ERRORS as exc:
        return _redirect("/ops", error=_error_text(exc), request=request)
    return _redirect("/ops", notice=f"Task {task_id} {'queued' if action == 'retry' else 'cancelled'}.", request=request)


def learning(request: Request) -> Response:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    with session_scope() as db:
        from govcon.learning.evals import run_eval_harness
        from govcon.models import AnalyticsSnapshot, OutcomeSuggestion

        analytics = outcome_analytics(db)
        last_refresh = db.scalar(select(AnalyticsSnapshot).order_by(AnalyticsSnapshot.id.desc()).limit(1))
        suggestion_rows = list(db.scalars(
            select(OutcomeSuggestion).order_by(OutcomeSuggestion.created_at.desc()).limit(8)
        ).all())
        capture_counts = {
            status: int(db.scalar(
                select(func.count()).select_from(OutcomeSuggestion).where(OutcomeSuggestion.status == status)
            ) or 0)
            for status in ("suggested", "confirmed", "dismissed")
        }
        capture_rows = [
            {
                "id": row.id,
                "opportunity_id": row.opportunity_id,
                "source": row.source,
                "strength": row.strength,
                "status": row.status,
                "suggested_outcome": row.suggested_outcome or "none suggested",
                "evidence_keys": sorted((row.evidence or {}).keys()),
            }
            for row in suggestion_rows
        ]
        evals = run_eval_harness()

    stats = ViewRow({
        "submitted": analytics.total_submitted,
        "won": analytics.total_won,
        "lost": analytics.total_lost,
        "no_bid": analytics.total_no_bid,
        "win_rate": analytics.overall_win_rate_pct or 0,
        "avg_margin": analytics.avg_margin_pct_on_wins,
        "avg_days_cycle": analytics.avg_days_discovery_to_submission,
        "win_profile_available": analytics.win_profile_available,
        "win_profile_note": analytics.win_profile_note,
        "win_profile_minimum": WIN_PROFILE_MINIMUM,
    })

    return _render(request, "learning.html", {
        "stats": stats,
        "last_refresh": last_refresh,
        "by_psc": analytics.by_psc,
        "by_agency": analytics.by_agency,
        "by_size": analytics.by_size_bucket,
        "no_bid_reasons": analytics.no_bid_reasons,
        "loss_reasons": analytics.loss_reasons,
        "common_competitors": analytics.common_competitors,
        "reliable_suppliers": analytics.reliable_suppliers,
        "recent_outcomes": analytics.recent_outcomes,
        "capture_counts": capture_counts,
        "capture_rows": capture_rows,
        "evals": evals,
        "active_page": "learning",
    }, user)
