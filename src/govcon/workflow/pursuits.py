"""The one way to start a pursuit (roadmap gap 4, ADR-067).

Web, CLI, MCP and the approval gate all call ``create_or_get_pursuit``. It
takes the opportunity lock, creates the pursuit once (a repeat returns the
existing one unchanged), audits the creation with its origin, and, when the
owner's setting allows, queues automatic preparation in the same commit.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.audit import record_audit
from govcon.diagnostics import trace_phase
from govcon.models import Pursuit, User
from govcon.workflow.invalidation import lock_opportunity

ORIGINS = ("web_inbox", "web_workspace", "mcp", "cli", "approval", "auto_policy")
# Stages a pursuit may start in; gated stages are entered only through their services.
START_STAGES = frozenset({"evaluating", "sourcing", "drafting", "review"})


@trace_phase("workflow.pursuits.create_or_get_pursuit")
def create_or_get_pursuit(
    session: Session,
    *,
    opportunity_id: int,
    actor: User | None,
    origin: str,
    stage: str = "evaluating",
    notes: str | None = None,
    prepare: bool = True,
) -> tuple[Pursuit, bool]:
    """Return ``(pursuit, created)``; an existing pursuit is returned unchanged."""
    if origin not in ORIGINS:
        raise ValueError(f"unknown pursuit origin {origin!r}")
    if stage not in START_STAGES:
        raise ValueError(f"a pursuit cannot start in stage {stage!r}")
    if lock_opportunity(session, opportunity_id) is None:
        raise ValueError(f"opportunity not found: {opportunity_id}")
    existing = session.scalar(select(Pursuit).where(Pursuit.opportunity_id == opportunity_id))
    if existing is not None:
        return existing, False
    pursuit = Pursuit(opportunity_id=opportunity_id, stage=stage, notes=notes)
    session.add(pursuit)
    session.flush()
    record_audit(
        session,
        action_type="pursuit_created",
        user_id=actor.id if actor else None,
        opportunity_id=opportunity_id,
        entity_type="pursuits",
        entity_id=pursuit.id,
        new_value={"stage": stage, "via": origin},
    )
    if prepare and origin != "approval":
        from govcon.workflow.app_settings import AUTO_PREPARE, get_setting
        from govcon.workflow.preparation import queue_preparation

        if get_setting(session, AUTO_PREPARE)["enabled"]:
            queue_preparation(session, opportunity_id=opportunity_id, actor_user_id=actor.id if actor else None)
    return pursuit, True
