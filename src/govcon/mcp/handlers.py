"""MCP tool handlers. Pure functions over a Session; FastMCP wraps these."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from govcon.audit import scrub
from govcon.collaboration.ai_comment_review import (
    apply_ai_validation_to_comment,
    validate_comment_with_ai,
)
from govcon.collaboration.assignments import assign_reviewer
from govcon.collaboration.comments import add_comment
from govcon.collaboration.review_sessions import complete_assignment, finalize_approval
from govcon.collaboration.users import PermissionDenied
from govcon.compliance.matrix import compliance_matrix, override_requirement
from govcon.compliance.submission_preflight import ReadinessBlocked, move_to_ready_to_submit
from govcon.intelligence.competitors import competitor_summary
from govcon.intelligence.vendors import vendor_profile
from govcon.matching.pricing import price_history, price_history_psc
from govcon.mcp import services
from govcon.mcp.serialize import err, not_implemented, ok, to_jsonable
from govcon.models import ReviewComment
from govcon.proposals.service import generate_proposal, get_proposal_workspace, record_submission_confirmation
from govcon.submissions.service import get_submission_workspace


def _decimal(value: str | float | int | None) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid decimal: {value}") from exc


def _handle(fn, *args, **kwargs) -> dict[str, Any]:
    try:
        return fn(*args, **kwargs)
    except PermissionDenied as exc:
        return err("PermissionDenied", str(exc))
    except ReadinessBlocked as exc:
        return err("ReadinessBlocked", "submission readiness blocked", blockers=exc.blockers)
    except ValueError as exc:
        return err("ValueError", str(exc))
    except Exception as exc:  # noqa: BLE001 — MCP must never leak stack traces
        return err(type(exc).__name__, str(exc) or "unexpected error")


# ── reads ──


def search_opportunities(
    session: Session,
    query: str | None = None,
    source: str | None = None,
    status: str | None = None,
    psc_prefix: str | None = None,
    naics_prefix: str | None = None,
    nsn: str | None = None,
    closing_within_days: int | None = None,
    limit: int = 25,
    include_full_description: bool = False,
) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        rows = services.search_opportunities(
            session,
            query=query,
            source=source,
            status=status,
            psc_prefix=psc_prefix,
            naics_prefix=naics_prefix,
            nsn=nsn,
            closing_within_days=closing_within_days,
            limit=limit,
            include_full_description=include_full_description,
        )
        return ok({"count": len(rows), "opportunities": rows})

    return _handle(run)


def get_opportunity(
    session: Session,
    opportunity_id: int,
    include_full_description: bool = False,
) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        row = services.get_opportunity(
            session, opportunity_id, include_full_description=include_full_description
        )
        if row is None:
            return err("NotFound", f"opportunity not found: {opportunity_id}")
        return ok(row)

    return _handle(run)


def get_opportunity_history(session: Session, opportunity_id: int, limit: int = 50) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        row = services.get_opportunity_history(session, opportunity_id, limit=limit)
        if row is None:
            return err("NotFound", f"opportunity not found: {opportunity_id}")
        return ok(row)

    return _handle(run)


def price_history_tool(
    session: Session,
    nsn: str | None = None,
    psc: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        if not nsn and not psc:
            return err("ValueError", "provide nsn and/or psc")
        points = []
        if nsn:
            points.extend(price_history(session, nsn, limit=limit))
        if psc:
            points.extend(price_history_psc(session, psc, limit=limit))
        return ok({"count": len(points), "prices": [to_jsonable(p) for p in points]})

    return _handle(run)


def vendor_profile_tool(
    session: Session,
    uei: str,
    refresh: bool = False,
) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        profile = vendor_profile(session, uei, refresh=refresh)
        return ok(to_jsonable(profile))

    return _handle(run)


def competitor_summary_tool(session: Session, opportunity_id: int, limit: int = 5) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        summary = competitor_summary(session, opportunity_id, limit=limit)
        if summary is None:
            return err("NotFound", f"opportunity not found: {opportunity_id}")
        return ok(to_jsonable(summary))

    return _handle(run)


def list_matches(
    session: Session,
    status: str | None = None,
    watchlist_id: int | None = None,
    closing_within_days: int | None = None,
    limit: int = 50,
    include_full_description: bool = False,
) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        rows = services.list_matches(
            session,
            status=status,
            watchlist_id=watchlist_id,
            closing_within_days=closing_within_days,
            limit=limit,
            include_full_description=include_full_description,
        )
        return ok({"count": len(rows), "matches": rows})

    return _handle(run)


def pipeline_summary(session: Session) -> dict[str, Any]:
    return _handle(lambda: ok(services.pipeline_summary(session)))


def get_bid_analysis(session: Session, opportunity_id: int) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        row = services.get_bid_analysis(session, opportunity_id)
        if row is None:
            return err("NotFound", f"opportunity not found: {opportunity_id}")
        return ok(row)

    return _handle(run)


def get_compliance_matrix(
    session: Session,
    opportunity_id: int,
    filters: list[str] | None = None,
) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        rows = compliance_matrix(session, opportunity_id, filters=tuple(filters or ()))
        return ok({"opportunity_id": opportunity_id, "count": len(rows), "rows": rows})

    return _handle(run)


def get_proposal(session: Session, opportunity_id: int) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        return ok(get_proposal_workspace(session, opportunity_id=opportunity_id))

    return _handle(run)


def submission_status(session: Session, opportunity_id: int) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        return ok(get_submission_workspace(session, opportunity_id=opportunity_id))

    return _handle(run)


def learning_summary(
    session: Session,  # noqa: ARG001 — kept for tool signature parity
    **_: Any,
) -> dict[str, Any]:
    return not_implemented(
        "learning_summary",
        15,
        "Outcome learning analytics arrive in Phase 15.",
    )


def similar_opportunities(
    session: Session,  # noqa: ARG001
    opportunity_id: int | None = None,  # noqa: ARG001
    **_: Any,
) -> dict[str, Any]:
    return not_implemented(
        "similar_opportunities",
        13,
        "Semantic similarity search arrives in Phase 13.",
    )


# ── writes ──


def update_match(
    session: Session,
    match_id: int,
    status: str,
    actor_email: str,
    confirm: bool = False,
) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        actor = services.resolve_actor(session, actor_email)
        changed = services.update_match(
            session, match_id=match_id, status=status, actor=actor, confirm=confirm
        )
        return ok(changed=changed)

    return _handle(run)


def add_pursuit(
    session: Session,
    opportunity_id: int,
    actor_email: str,
    stage: str = "evaluating",
    notes: str | None = None,
    sourcing_cost: str | None = None,
    quote_price: str | None = None,
    supplier: str | None = None,
) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        actor = services.resolve_actor(session, actor_email)
        changed = services.add_pursuit(
            session,
            opportunity_id=opportunity_id,
            actor=actor,
            stage=stage,
            notes=notes,
            sourcing_cost=_decimal(sourcing_cost),
            quote_price=_decimal(quote_price),
            supplier=supplier,
        )
        return ok(changed=changed)

    return _handle(run)


def update_pursuit(
    session: Session,
    pursuit_id: int,
    actor_email: str,
    expected_version: int,
    stage: str | None = None,
    notes: str | None = None,
    sourcing_cost: str | None = None,
    quote_price: str | None = None,
    supplier: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        actor = services.resolve_actor(session, actor_email)
        changed = services.update_pursuit(
            session,
            pursuit_id=pursuit_id,
            actor=actor,
            expected_version=expected_version,
            stage=stage,
            notes=notes,
            sourcing_cost=_decimal(sourcing_cost),
            quote_price=_decimal(quote_price),
            supplier=supplier,
            confirm=confirm,
        )
        return ok(changed=changed)

    return _handle(run)


def record_human_bid_decision(
    session: Session,
    opportunity_id: int,
    actor_email: str,
    human_decision: str,
    human_comments: str | None = None,
    bid_decision_id: int | None = None,
) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        actor = services.resolve_actor(session, actor_email)
        changed = services.record_human_bid_decision(
            session,
            opportunity_id=opportunity_id,
            actor=actor,
            human_decision=human_decision,
            human_comments=human_comments,
            bid_decision_id=bid_decision_id,
        )
        return ok(changed=changed)

    return _handle(run)


def assign_reviewer_tool(
    session: Session,
    opportunity_id: int,
    user_id: int,
    actor_email: str | None = None,
    assignment_role: str = "reviewer",
) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        actor_id = None
        if actor_email:
            actor_id = services.resolve_actor(session, actor_email).id
        row = assign_reviewer(
            session,
            opportunity_id=opportunity_id,
            user_id=user_id,
            assignment_role=assignment_role,
            actor_user_id=actor_id,
        )
        return ok(
            changed={
                "assignment_id": row.id,
                "opportunity_id": row.opportunity_id,
                "user_id": row.user_id,
                "assignment_role": row.assignment_role,
                "status": row.status,
            }
        )

    return _handle(run)


def add_review_comment(
    session: Session,
    opportunity_id: int,
    actor_email: str,
    body: str,
    topic: str | None = None,
    parent_comment_id: int | None = None,
    user_recommendation: str | None = None,
    validate_with_ai: bool = False,
) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        actor = services.resolve_actor(session, actor_email)
        row = add_comment(
            session,
            opportunity_id=opportunity_id,
            user_id=actor.id,
            body=body,
            topic=topic,
            parent_comment_id=parent_comment_id,
            user_recommendation=user_recommendation,
            validate_with_ai=validate_with_ai,
        )
        return ok(
            changed={
                "comment_id": row.id,
                "opportunity_id": row.opportunity_id,
                "user_id": row.user_id,
                "body": row.body,
                "topic": row.topic,
                "ai_position": row.ai_position,
            }
        )

    return _handle(run)


def complete_review(
    session: Session,
    opportunity_id: int,
    actor_email: str,
    action: str,
    recommendation: str | None = None,
    agree_with_ai_assessment: bool | None = None,
    second_review_reason: str | None = None,
) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        actor = services.resolve_actor(session, actor_email)
        row = complete_assignment(
            session,
            opportunity_id=opportunity_id,
            user_id=actor.id,
            action=action,
            recommendation=recommendation,
            agree_with_ai_assessment=agree_with_ai_assessment,
            second_review_reason=second_review_reason,
        )
        return ok(
            changed={
                "assignment_id": row.id,
                "opportunity_id": row.opportunity_id,
                "user_id": row.user_id,
                "status": row.status,
                "recommendation": row.recommendation,
            }
        )

    return _handle(run)


def request_ai_comment_validation(
    session: Session,
    comment_id: int,
    actor_email: str | None = None,  # noqa: ARG001 — attribution reserved for audit callers
) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        comment = session.get(ReviewComment, comment_id)
        if comment is None:
            return err("NotFound", f"comment not found: {comment_id}")
        validation = validate_comment_with_ai(session, comment=comment)
        apply_ai_validation_to_comment(comment, validation)
        session.flush()
        return ok(
            changed={
                "comment_id": comment.id,
                "ai_position": comment.ai_position,
                "ai_confidence": comment.ai_confidence,
                "ai_reason": comment.ai_reason,
            }
        )

    return _handle(run)


def approve_to_bid(
    session: Session,
    opportunity_id: int,
    actor_email: str,
    expected_version: int | None = None,
    override_reason: str | None = None,
) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        actor = services.resolve_actor(session, actor_email)
        review = finalize_approval(
            session,
            opportunity_id=opportunity_id,
            actor=actor,
            action="approve_to_bid",
            expected_version=expected_version,
            override_reason=override_reason,
        )
        return ok(
            changed={
                "review_session_id": review.id,
                "opportunity_id": review.opportunity_id,
                "status": review.status,
                "final_approval_status": review.final_approval_status,
                "version": review.version,
            }
        )

    return _handle(run)


def update_requirement_status(
    session: Session,
    requirement_id: int,
    status: str,
    actor_email: str,
    reason: str,
    expected_version: int,
    acknowledge_deterministic_failure: bool = False,
) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        actor = services.resolve_actor(session, actor_email)
        row = override_requirement(
            session,
            requirement_id=requirement_id,
            status=status,
            actor=actor,
            reason=reason,
            expected_version=expected_version,
            acknowledge_deterministic_failure=acknowledge_deterministic_failure,
        )
        return ok(
            changed={
                "requirement_id": row.id,
                "opportunity_id": row.opportunity_id,
                "status": row.status,
                "status_reason": row.status_reason,
                "version": row.version,
                "blocks_submission": row.blocks_submission,
            }
        )

    return _handle(run)


def create_proposal_version(
    session: Session,
    opportunity_id: int,
    actor_email: str | None = None,
    skip_ai: bool = True,
) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        actor = services.resolve_actor(session, actor_email) if actor_email else None
        result = generate_proposal(
            session,
            opportunity_id=opportunity_id,
            actor=actor,
            skip_ai=skip_ai,
        )
        return ok(changed=scrub(to_jsonable(result)))

    return _handle(run)


def set_submission_ready(
    session: Session,
    opportunity_id: int,
    actor_email: str,
    override_reason: str | None = None,
) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        actor = services.resolve_actor(session, actor_email)
        pursuit = move_to_ready_to_submit(
            session,
            opportunity_id,
            actor=actor,
            override_reason=override_reason,
        )
        return ok(
            changed={
                "pursuit_id": pursuit.id,
                "opportunity_id": pursuit.opportunity_id,
                "stage": pursuit.stage,
                "version": pursuit.version,
            }
        )

    return _handle(run)


def record_submission_confirmation_tool(
    session: Session,
    opportunity_id: int,
    actor_email: str,
    confirmation_number: str | None = None,
    confirmation_notes: str | None = None,
) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        actor = services.resolve_actor(session, actor_email)
        result = record_submission_confirmation(
            session,
            opportunity_id=opportunity_id,
            confirmation_number=confirmation_number,
            confirmation_notes=confirmation_notes,
            actor=actor,
        )
        # Never auto-submit portals; this only records human confirmation.
        return ok(changed=result, auto_submitted=False)

    return _handle(run)


def record_outcome(
    session: Session,  # noqa: ARG001
    opportunity_id: int | None = None,  # noqa: ARG001
    **_: Any,
) -> dict[str, Any]:
    return not_implemented(
        "record_outcome",
        15,
        "Outcome capture arrives in Phase 15.",
    )
