"""Read-only progress snapshots; provider work stays in the worker."""

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, literal, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.orm import Session

from govcon.db import session_scope
from govcon.models import Opportunity, Task
from govcon.tasks.queue import ACTIVE_STATUSES


def progress_state(db: Session, opp_id: int) -> dict[str, Any]:
    # Aggregate in PostgreSQL so old active work stays visible without loading
    # an unbounded task history into the web process.
    fingerprint = func.concat(Task.id, ":", Task.status, ":", Task.checkpoint, ":", Task.result,
                              ":", Task.last_error, ":", Task.blocker_next_action)
    revision, active = db.execute(select(
        func.md5(func.string_agg(fingerprint, aggregate_order_by(literal(","), Task.id))),
        func.bool_or(Task.status.in_(ACTIVE_STATUSES)),
    ).where(Task.opportunity_id == opp_id, Task.task_type.in_(
        ("opportunity_preparation", "ai_analysis", "proposal_generation", "quote_extraction", "solicitation_summary")))).one()
    return {"revision": revision or "empty", "active": bool(active)}


def workspace_progress(request: Request, opp_id: int) -> Response:
    from govcon.web.routes import _NeedsLogin, _require_login

    try:
        _require_login(request)
    except _NeedsLogin:
        return JSONResponse({"error": "Sign in to see progress."}, status_code=401)
    with session_scope() as db:
        if db.get(Opportunity, opp_id) is None:
            return JSONResponse({"error": "Opportunity not found."}, status_code=404)
        return JSONResponse(progress_state(db, opp_id), headers={"Cache-Control": "no-store"})
