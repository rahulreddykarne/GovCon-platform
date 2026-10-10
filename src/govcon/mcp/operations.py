"""MCP tool operations over existing GovCon services (no new product behavior).

Write tools act as the single MCP actor pinned at server start
(``MCP_ACTOR_EMAIL``), go through the same services as the web UI and CLI,
check that actor's role permission, and write audit rows.

Human-authority decisions are not exposed: approving a bid, completing a
human review, recording a human bid decision, overriding a compliance status,
moving a pursuit to ready_to_submit, and recording a submission are done by a
person in the web UI or CLI.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, func, or_, select, text
from sqlalchemy.orm import Session

from govcon.ai.analysis_types import AnalysisType
from govcon.audit import record_audit
from govcon.collaboration.ai_comment_review import (
    AICommentValidationError,
    apply_ai_validation_to_comment,
    validate_comment_with_ai,
)
from govcon.collaboration.assignments import assign_reviewer
from govcon.collaboration.comments import add_comment
from govcon.collaboration.users import require_permission
from govcon.compliance.matrix import active_requirements
from govcon.compliance.metrics import coverage_summary
from govcon.concurrency import StaleRecordError, apply_versioned_update
from govcon.decision.engine import list_decision_runs
from govcon.intelligence.competitors import competitor_summary
from govcon.intelligence.vendors import vendor_profile
from govcon.matching.pricing import price_history, price_history_psc
from govcon.mcp.context import current_actor
from govcon.mcp.serialize import compact_opportunity, json_safe, success, truncate_text
from govcon.models import (
    AIAnalysis,
    BidDecision,
    Match,
    Opportunity,
    OpportunityEvent,
    OpportunitySnapshot,
    OutcomeFeedback,
    Proposal,
    Pursuit,
    Requirement,
    ReviewComment,
    User,
    Watchlist,
)
from govcon.proposals.service import get_proposal_workspace
from govcon.proposals.versions import create_proposal_version
from govcon.submissions.service import get_submission_workspace
from govcon.workflow.transitions import require_transition

_MATCH_STATUSES = frozenset({"new", "seen", "dismissed", "reviewing", "pursuing"})
_PURSUIT_STAGES = frozenset(
    {
        "evaluating",
        "bid_approved",
        "sourcing",
        "drafting",
        "review",
        "ready_to_submit",
        "submitted",
        "won",
        "lost",
        "cancelled",
        "no_bid",
    }
)
# Stages an MCP client may set directly. Gated stages (bid_approved, no_bid,
# ready_to_submit, submitted, won, lost) and terminal cancellation are human or
# service-gated; see govcon.workflow.transitions.
MCP_SETTABLE_PURSUIT_STAGES = frozenset({"evaluating", "sourcing", "drafting", "review"})


def _require_row(session: Session, model: type, row_id: int, label: str) -> Any:
    row = session.get(model, row_id)
    if row is None:
        raise ValueError(f"{label} {row_id} not found")
    return row


def _latest_bid_decision(session: Session, opportunity_id: int) -> BidDecision | None:
    return session.scalar(
        select(BidDecision)
        .where(BidDecision.opportunity_id == opportunity_id)
        .order_by(desc(BidDecision.created_at), desc(BidDecision.id))
        .limit(1)
    )


def _latest_decision_package(session: Session, opportunity_id: int) -> AIAnalysis | None:
    return session.scalar(
        select(AIAnalysis)
        .where(
            AIAnalysis.opportunity_id == opportunity_id,
            AIAnalysis.analysis_type == AnalysisType.DECISION_PACKAGE,
        )
        .order_by(desc(AIAnalysis.created_at), desc(AIAnalysis.id))
        .limit(1)
    )


# ── read tools ──


def op_search_opportunities(
    session: Session,
    *,
    query: str | None = None,
    source: str | None = None,
    psc_prefix: str | None = None,
    naics_prefix: str | None = None,
    status: str | None = None,
    closing_within_days: int | None = None,
    limit: int = 25,
    include_full_description: bool = False,
) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    stmt = select(Opportunity)
    if source:
        stmt = stmt.where(Opportunity.source == source)
    if status:
        stmt = stmt.where(Opportunity.status == status)
    if psc_prefix:
        stmt = stmt.where(Opportunity.psc_code.ilike(f"{psc_prefix}%"))
    if naics_prefix:
        stmt = stmt.where(Opportunity.naics_code.ilike(f"{naics_prefix}%"))
    if closing_within_days is not None:
        cutoff = datetime.now(UTC) + timedelta(days=closing_within_days)
        stmt = stmt.where(
            Opportunity.response_deadline.is_not(None),
            Opportunity.response_deadline <= cutoff,
            Opportunity.response_deadline >= datetime.now(UTC),
        )
    if query and query.strip():
        q = query.strip()
        stmt = stmt.where(
            or_(
                Opportunity.title.ilike(f"%{q}%"),
                Opportunity.solicitation_number.ilike(f"%{q}%"),
                Opportunity.nsn.ilike(f"%{q}%"),
                text(
                    "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(description, '')) "
                    "@@ plainto_tsquery('english', :q)"
                ).bindparams(q=q),
            )
        )
    rows = session.scalars(
        stmt.order_by(Opportunity.response_deadline.asc().nullslast(), Opportunity.id.desc()).limit(limit)
    ).all()
    return success(
        {
            "count": len(rows),
            "opportunities": [
                compact_opportunity(row, include_full_description=include_full_description)
                for row in rows
            ],
        }
    )


def op_get_opportunity(
    session: Session,
    opportunity_id: int,
    *,
    include_full_description: bool = False,
) -> dict[str, Any]:
    row = _require_row(session, Opportunity, opportunity_id, "opportunity")
    return success(compact_opportunity(row, include_full_description=include_full_description))


def op_get_opportunity_history(session: Session, opportunity_id: int) -> dict[str, Any]:
    _require_row(session, Opportunity, opportunity_id, "opportunity")
    snapshots = session.scalars(
        select(OpportunitySnapshot)
        .where(OpportunitySnapshot.opportunity_id == opportunity_id)
        .order_by(OpportunitySnapshot.fetched_at, OpportunitySnapshot.id)
    ).all()
    events = session.scalars(
        select(OpportunityEvent)
        .where(OpportunityEvent.opportunity_id == opportunity_id)
        .order_by(OpportunityEvent.detected_at, OpportunityEvent.id)
    ).all()
    return success(
        {
            "opportunity_id": opportunity_id,
            "snapshots": [
                {
                    "id": s.id,
                    "fetched_at": s.fetched_at,
                    "content_hash": s.content_hash,
                    "source_version": s.source_version,
                }
                for s in snapshots
            ],
            "events": [
                {
                    "id": e.id,
                    "event_type": e.event_type,
                    "field_name": e.field_name,
                    "old_value": e.old_value,
                    "new_value": e.new_value,
                    "snapshot_id": e.snapshot_id,
                    "detected_at": e.detected_at,
                }
                for e in events
            ],
        }
    )


def op_price_history(
    session: Session,
    *,
    nsn: str | None = None,
    psc: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    if nsn:
        points = price_history(session, nsn, limit=limit)
        return success(
            {
                "nsn": nsn,
                "count": len(points),
                "points": [json_safe(asdict(p) if is_dataclass(p) else p) for p in points],
            }
        )
    if psc:
        points = price_history_psc(session, psc, limit=limit)
        return success(
            {
                "psc": psc,
                "count": len(points),
                "points": [json_safe(asdict(p) if is_dataclass(p) else p) for p in points],
            }
        )
    raise ValueError("provide nsn or psc")


def op_vendor_profile(session: Session, uei: str) -> dict[str, Any]:
    profile = vendor_profile(session, uei)
    return success(json_safe(asdict(profile)))


def op_competitor_summary(session: Session, opportunity_id: int) -> dict[str, Any]:
    _require_row(session, Opportunity, opportunity_id, "opportunity")
    summary = competitor_summary(session, opportunity_id)
    if summary is None:
        return success({"opportunity_id": opportunity_id, "buckets": []})
    return success(json_safe(asdict(summary)))


def op_list_matches(
    session: Session,
    *,
    status: str | None = None,
    watchlist_id: int | None = None,
    closing_within_days: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    limit = max(1, min(limit, 200))
    stmt = (
        select(Match, Opportunity, Watchlist)
        .join(Opportunity, Match.opportunity_id == Opportunity.id)
        .join(Watchlist, Match.watchlist_id == Watchlist.id)
        # Matches the watchlist no longer produces are history, not work.
        .where(Match.active.is_(True))
    )
    if status:
        if status not in _MATCH_STATUSES:
            raise ValueError(f"status must be one of {sorted(_MATCH_STATUSES)}")
        stmt = stmt.where(Match.status == status)
    if watchlist_id is not None:
        stmt = stmt.where(Match.watchlist_id == watchlist_id)
    if closing_within_days is not None:
        cutoff = datetime.now(UTC) + timedelta(days=closing_within_days)
        stmt = stmt.where(
            Opportunity.response_deadline.is_not(None),
            Opportunity.response_deadline <= cutoff,
            Opportunity.response_deadline >= datetime.now(UTC),
        )
    rows = session.execute(
        stmt.order_by(Opportunity.response_deadline.asc().nullslast(), Match.id.desc()).limit(limit)
    ).all()
    payload = []
    for match, opp, watchlist in rows:
        payload.append(
            {
                "match_id": match.id,
                "status": match.status,
                "score": match.score,
                "watchlist_id": watchlist.id,
                "watchlist_name": watchlist.name,
                "opportunity": compact_opportunity(opp),
                "matched_on": match.matched_on,
            }
        )
    return success({"count": len(payload), "matches": payload})


def op_pipeline_summary(session: Session) -> dict[str, Any]:
    counts = dict(
        session.execute(
            select(Pursuit.stage, func.count()).group_by(Pursuit.stage)
        ).all()
    )
    rows = session.execute(
        select(Pursuit, Opportunity)
        .join(Opportunity, Pursuit.opportunity_id == Opportunity.id)
        .order_by(Pursuit.updated_at.desc().nullslast(), Pursuit.id.desc())
        .limit(100)
    ).all()
    items = [
        {
            "pursuit_id": pursuit.id,
            "stage": pursuit.stage,
            "opportunity_id": opp.id,
            "title": opp.title,
            "response_deadline": opp.response_deadline,
        }
        for pursuit, opp in rows
    ]
    return success({"stage_counts": counts, "recent": items})


def op_get_bid_analysis(session: Session, opportunity_id: int) -> dict[str, Any]:
    _require_row(session, Opportunity, opportunity_id, "opportunity")
    bid = _latest_bid_decision(session, opportunity_id)
    package = _latest_decision_package(session, opportunity_id)
    runs = list_decision_runs(session, opportunity_id=opportunity_id, limit=5)
    if bid is None and package is None:
        raise ValueError(f"no bid analysis for opportunity {opportunity_id}")
    return success(
        {
            "opportunity_id": opportunity_id,
            "bid_decision": None
            if bid is None
            else {
                "id": bid.id,
                "recommendation": bid.recommendation,
                "recommendation_score": bid.recommendation_score,
                "capability_score": bid.capability_score,
                "pricing_score": bid.pricing_score,
                "compliance_risk_score": bid.compliance_risk_score,
                "strengths": bid.strengths,
                "risks": bid.risks,
                "missing_information": bid.missing_information,
                "evidence": bid.evidence,
                "human_decision": bid.human_decision,
                "human_comments": bid.human_comments,
                "created_at": bid.created_at,
            },
            "decision_package": package.output_json if package else None,
            "recent_bundle_runs": [
                {
                    "id": run.id,
                    "bundle_name": run.bundle_name,
                    "provider": run.provider,
                    "status": run.result.get("status"),
                    "created_at": run.created_at,
                }
                for run in runs
            ],
        }
    )


def op_get_compliance_matrix(
    session: Session,
    opportunity_id: int,
    *,
    include_requirement_text: bool = False,
) -> dict[str, Any]:
    _require_row(session, Opportunity, opportunity_id, "opportunity")
    requirements = active_requirements(session, opportunity_id=opportunity_id)
    summary = coverage_summary(requirements)
    rows = []
    for req in requirements:
        text_value: str | None = req.requirement_text
        if not include_requirement_text:
            shortened = truncate_text(text_value, limit=240)
            if shortened is not None:
                text_value = shortened
        rows.append(
            {
                "id": req.id,
                "requirement_type": req.requirement_type,
                "status": req.status,
                "mandatory": req.mandatory,
                "severity": req.severity,
                "blocks_submission": req.blocks_submission,
                "stale_due_to_amendment": req.stale_due_to_amendment,
                "status_reason": truncate_text(req.status_reason, limit=240),
                "requirement_text": text_value,
                "version": req.version,
            }
        )
    missing = [
        {
            "id": r["id"],
            "requirement_type": r["requirement_type"],
            "status": r["status"],
            "requirement_text": r["requirement_text"],
        }
        for r in rows
        if r["mandatory"] and r["status"] in {"missing", "unknown", "needs_review", "stale"}
    ]
    return success(
        {
            "opportunity_id": opportunity_id,
            "summary": summary,
            "requirements": rows,
            "missing_or_unclear": missing,
        }
    )


def op_get_proposal(
    session: Session,
    opportunity_id: int,
    *,
    include_full_text: bool = False,
) -> dict[str, Any]:
    workspace = get_proposal_workspace(session, opportunity_id=opportunity_id)
    if not include_full_text:
        workspace.pop("sections", None)
    proposal = session.scalars(
        select(Proposal).where(Proposal.opportunity_id == opportunity_id)
    ).first()
    if proposal and proposal.current_version_id and include_full_text:
        from govcon.proposals.versions import (
            get_sections_for_version,
            latest_proposal_version,
        )

        version = latest_proposal_version(session, proposal.id)
        sections = get_sections_for_version(session, proposal.current_version_id)
        workspace["sections"] = [
            {
                "section_key": s.section_key,
                "heading": s.heading,
                "content": s.content if include_full_text else truncate_text(s.content, limit=500),
                "status": s.status,
            }
            for s in sections
        ]
        if version and not include_full_text:
            workspace["full_text_preview"] = truncate_text(version.full_text, limit=500)
    return success(workspace)


def op_submission_status(session: Session, opportunity_id: int) -> dict[str, Any]:
    _require_row(session, Opportunity, opportunity_id, "opportunity")
    return success(get_submission_workspace(session, opportunity_id=opportunity_id))


def op_learning_summary(session: Session, *, opportunity_id: int | None = None) -> dict[str, Any]:
    from govcon.learning.analytics import (
        WIN_PROFILE_MINIMUM,
        outcome_analytics,
        similar_past_outcomes,
    )
    from govcon.learning.outcomes import outcome_to_dict

    if opportunity_id is not None:
        # Return record-level summary + similar past outcomes for decision reports
        _require_row(session, Opportunity, opportunity_id, "opportunity")
        rows = session.scalars(
            select(OutcomeFeedback)
            .where(OutcomeFeedback.opportunity_id == opportunity_id)
            .order_by(desc(OutcomeFeedback.updated_at), desc(OutcomeFeedback.id))
        ).all()
        similar = similar_past_outcomes(session, opportunity_id, limit=5)
        return success(
            {
                "opportunity_id": opportunity_id,
                "records": [outcome_to_dict(r) for r in rows],
                "similar_past_outcomes": similar,
                "note": (
                    "similar_past_outcomes are descriptive references; "
                    "they do not establish causation."
                ),
            }
        )

    # Global analytics summary
    analytics = outcome_analytics(session)
    return success(
        {
            "total_submitted": analytics.total_submitted,
            "total_won": analytics.total_won,
            "total_lost": analytics.total_lost,
            "total_no_bid": analytics.total_no_bid,
            "overall_win_rate_pct": analytics.overall_win_rate_pct,
            "avg_margin_pct_on_wins": analytics.avg_margin_pct_on_wins,
            "avg_days_discovery_to_submission": analytics.avg_days_discovery_to_submission,
            "win_profile_available": analytics.win_profile_available,
            "win_profile_note": analytics.win_profile_note,
            "by_psc": [
                {
                    "psc": r.key,
                    "submitted": r.submitted,
                    "won": r.won,
                    "lost": r.lost,
                    "win_rate_pct": r.win_rate_pct,
                    "small_sample": r.small_sample,
                }
                for r in analytics.by_psc
            ],
            "by_agency": [
                {
                    "agency": r.key,
                    "submitted": r.submitted,
                    "won": r.won,
                    "lost": r.lost,
                    "win_rate_pct": r.win_rate_pct,
                    "small_sample": r.small_sample,
                }
                for r in analytics.by_agency
            ],
            "no_bid_reasons": [
                {"reason": r.reason, "count": r.count} for r in analytics.no_bid_reasons
            ],
            "loss_reasons": [
                {"reason": r.reason, "count": r.count} for r in analytics.loss_reasons
            ],
            "common_competitors": [
                {"competitor": r.reason, "times_lost_to": r.count}
                for r in analytics.common_competitors
            ],
            "reliable_suppliers": [
                {"supplier": r.supplier, "wins": r.wins} for r in analytics.reliable_suppliers
            ],
            "recent_outcomes": analytics.recent_outcomes[:10],
            "analytics_note": (
                "All figures are descriptive history. "
                f"Win-profile recommendations require ≥{WIN_PROFILE_MINIMUM} wins. "
                "Small-sample rows are labeled; interpret with caution."
            ),
        }
    )


def op_similar_opportunities(
    session: Session,
    opportunity_id: int,
    *,
    limit: int = 10,
) -> dict[str, Any]:
    from govcon.matching.semantic import similar_opportunities as _similar

    result = _similar(session, opportunity_id, limit=limit)
    if not result.get("ok", True):
        return result
    return success(result)


# ── write tools ──


def op_update_match(
    session: Session,
    match_id: int,
    *,
    status: str,
    confirm: bool = False,
) -> dict[str, Any]:
    if status not in _MATCH_STATUSES:
        raise ValueError(f"status must be one of {sorted(_MATCH_STATUSES)}")
    actor = current_actor(session, "review")
    match = _require_row(session, Match, match_id, "match")
    if status == "dismissed" and not confirm:
        raise ValueError("set confirm=true to dismiss a match")
    old_status = match.status
    match.status = status
    session.flush()
    record_audit(
        session,
        action_type="match_status_changed",
        user_id=actor.id,
        opportunity_id=match.opportunity_id,
        entity_type="matches",
        entity_id=match.id,
        old_value={"status": old_status},
        new_value={"status": status, "via": "mcp"},
    )
    return success(
        {
            "match_id": match.id,
            "opportunity_id": match.opportunity_id,
            "watchlist_id": match.watchlist_id,
            "old_status": old_status,
            "status": match.status,
        }
    )


def op_add_pursuit(
    session: Session,
    opportunity_id: int,
    *,
    stage: str = "evaluating",
    notes: str | None = None,
) -> dict[str, Any]:
    if stage not in MCP_SETTABLE_PURSUIT_STAGES:
        raise ValueError(f"stage must be one of {sorted(MCP_SETTABLE_PURSUIT_STAGES)}")
    """Start a pursuit, or return the existing one unchanged (``created`` says which).

    Same service as the web and CLI (ADR-067), so preparation is queued the
    same way; a repeat call is not an error.
    """
    from govcon.workflow.pursuits import create_or_get_pursuit

    actor = current_actor(session, "review")
    _require_row(session, Opportunity, opportunity_id, "opportunity")
    pursuit, created = create_or_get_pursuit(
        session, opportunity_id=opportunity_id, actor=actor, origin="mcp", stage=stage, notes=notes
    )
    return success({**_pursuit_record(pursuit), "created": created})


def op_update_pursuit(
    session: Session,
    opportunity_id: int,
    *,
    expected_version: int,
    stage: str | None = None,
    quote_price: float | None = None,
    sourcing_cost: float | None = None,
    supplier: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Edit pre-submission working facts; edits revoke dependent decisions."""
    actor = current_actor(session, "review")
    from govcon.workflow.commercial import (
        LOCKED_STAGES,
        invalidate_commercial_decisions,
        validated_commercial_changes,
    )
    from govcon.workflow.invalidation import lock_one
    lock_one(session, select(Opportunity).where(Opportunity.id == opportunity_id))
    pursuit = lock_one(session, select(Pursuit).where(Pursuit.opportunity_id == opportunity_id))
    if pursuit is None:
        raise ValueError(f"no pursuit for opportunity {opportunity_id}")
    if pursuit.version != expected_version:
        raise ValueError("stale pursuit version; reload the pursuit")
    changes: dict[str, Any] = {}
    if stage is not None and stage != pursuit.stage:
        if stage not in MCP_SETTABLE_PURSUIT_STAGES:
            raise ValueError(
                f"stage {stage!r} is gated; MCP may set only {sorted(MCP_SETTABLE_PURSUIT_STAGES)}"
            )
        require_transition("pursuit", pursuit.stage, stage)
        changes["stage"] = stage
    if quote_price is not None:
        changes["quote_price"] = quote_price
    if sourcing_cost is not None:
        changes["sourcing_cost"] = sourcing_cost
    if supplier is not None:
        changes["supplier"] = supplier
    if notes is not None:
        changes["notes"] = notes
    commercial = {key: value for key, value in changes.items() if key != "stage"}
    if commercial:
        commercial = validated_commercial_changes(commercial)
        if pursuit.stage in LOCKED_STAGES:
            raise ValueError("commercial facts are locked in this stage; an approver must record an append-only correction for submitted facts")
        changes.update(commercial)
    changes = {key: value for key, value in changes.items() if getattr(pursuit, key) != value}
    substantive = bool(set(changes) - {"stage"})
    if not changes:
        return success(_pursuit_record(pursuit))
    try:
        previous = apply_versioned_update(session, pursuit, expected_version, changes)
    except StaleRecordError as exc:
        raise ValueError(f"{exc}; reload the pursuit") from exc
    record_audit(
        session,
        action_type="pursuit_updated",
        user_id=actor.id,
        opportunity_id=opportunity_id,
        entity_type="pursuits",
        entity_id=pursuit.id,
        old_value={k: (str(v) if isinstance(v, Decimal) else v) for k, v in previous.items()},
        new_value={**{k: (str(v) if isinstance(v, Decimal) else v) for k, v in changes.items()}, "via": "mcp"},
    )
    if substantive:
        invalidate_commercial_decisions(session, opportunity_id, actor=actor)
    return success(_pursuit_record(pursuit))


def op_assign_reviewer(
    session: Session,
    opportunity_id: int,
    *,
    user_email: str,
    assignment_role: str = "reviewer",
) -> dict[str, Any]:
    """Assign ``user_email`` (the reviewer, not the actor) to review an opportunity."""
    actor = current_actor(session, "approve")
    _require_row(session, Opportunity, opportunity_id, "opportunity")
    reviewer = session.scalar(
        select(User).where(User.email == user_email.strip().lower(), User.is_active.is_(True))
    )
    if reviewer is None:
        raise ValueError(f"active user not found for email {user_email}")
    require_permission(reviewer, "review")
    row = assign_reviewer(
        session,
        opportunity_id=opportunity_id,
        user_id=reviewer.id,
        assignment_role=assignment_role,
        actor_user_id=actor.id,
    )
    return success(_assignment_record(row))


def op_add_review_comment(
    session: Session,
    opportunity_id: int,
    *,
    body: str,
    topic: str | None = None,
    validate_with_ai: bool = True,
) -> dict[str, Any]:
    """Add a comment authored by the MCP actor (who must be an assigned reviewer)."""
    actor = current_actor(session, "review")
    row = add_comment(
        session,
        opportunity_id=opportunity_id,
        user_id=actor.id,
        body=body,
        topic=topic,
        validate_with_ai=validate_with_ai,
    )
    return success(_comment_record(row))


def op_request_ai_comment_validation(session: Session, comment_id: int) -> dict[str, Any]:
    actor = current_actor(session, "review")
    comment = _require_row(session, ReviewComment, comment_id, "comment")
    try:
        validation = validate_comment_with_ai(session, comment=comment)
        apply_ai_validation_to_comment(comment, validation)
    except AICommentValidationError as exc:
        raise ValueError(f"AI validation failed: {exc.reason}: {exc.detail}") from exc
    session.flush()
    record_audit(
        session,
        action_type="review_comment_ai_validated",
        user_id=actor.id,
        opportunity_id=comment.opportunity_id,
        entity_type="review_comments",
        entity_id=comment.id,
        new_value={"ai_position": comment.ai_position, "via": "mcp"},
    )
    return success(_comment_record(comment))


def op_create_proposal_version(
    session: Session,
    opportunity_id: int,
    *,
    sections: list[dict[str, Any]],
    change_summary: str | None = None,
) -> dict[str, Any]:
    """Save a new draft version; any prior final approval is invalidated."""
    actor = current_actor(session, "review")
    proposal = session.scalars(
        select(Proposal).where(Proposal.opportunity_id == opportunity_id)
    ).first()
    if proposal is None:
        raise ValueError(f"no proposal for opportunity {opportunity_id}")
    version = create_proposal_version(
        session,
        proposal_id=proposal.id,
        created_by=actor.email,
        sections=sections,
        change_summary=change_summary,
        actor_id=actor.id,
    )
    return success(
        {
            "proposal_id": proposal.id,
            "version_id": version.id,
            "version_number": version.version_number,
            "current_version_id": proposal.current_version_id,
            "proposal_status": proposal.status,
        }
    )


def op_record_outcome(
    session: Session,
    opportunity_id: int,
    *,
    outcome: str,
    # No-bid fields
    no_bid_reason: str | None = None,
    no_bid_category: str | None = None,
    # Loss fields
    loss_reason: str | None = None,
    known_winning_price: float | None = None,
    # Win fields
    win_reason: str | None = None,
    win_margin_pct: float | None = None,
    win_supplier: str | None = None,
    win_delivery_terms: str | None = None,
    win_proposal_version: str | None = None,
    # Common fields
    awarded_vendor_uei: str | None = None,
    awarded_vendor_name: str | None = None,
    award_amount: float | None = None,
    government_feedback: str | None = None,
    debrief_notes: str | None = None,
    lessons_learned: str | None = None,
) -> dict[str, Any]:
    from govcon.learning.outcomes import (
        NO_BID_CATEGORIES,
        TERMINAL_OUTCOMES,
        outcome_to_dict,
        record_outcome,
    )

    # Validate early (before actor lookup) so errors are clear
    if outcome not in TERMINAL_OUTCOMES:
        raise ValueError(f"outcome must be one of {sorted(TERMINAL_OUTCOMES)}")
    if no_bid_category is not None and no_bid_category not in NO_BID_CATEGORIES:
        raise ValueError(
            f"no_bid_category must be one of {sorted(NO_BID_CATEGORIES)} or None"
        )

    actor = current_actor(session, "approve")
    row = record_outcome(
        session,
        opportunity_id=opportunity_id,
        outcome=outcome,
        actor=actor,
        no_bid_reason=no_bid_reason,
        no_bid_category=no_bid_category,
        loss_reason=loss_reason,
        known_winning_price=known_winning_price,
        win_reason=win_reason,
        win_margin_pct=win_margin_pct,
        win_supplier=win_supplier,
        win_delivery_terms=win_delivery_terms,
        win_proposal_version=win_proposal_version,
        awarded_vendor_uei=awarded_vendor_uei,
        awarded_vendor_name=awarded_vendor_name,
        award_amount=award_amount,
        government_feedback=government_feedback,
        debrief_notes=debrief_notes,
        lessons_learned=lessons_learned,
    )
    pursuit = session.scalar(select(Pursuit).where(Pursuit.opportunity_id == opportunity_id))
    result = outcome_to_dict(row)
    result["pursuit_stage"] = pursuit.stage if pursuit else None
    result["recorded_by"] = actor.email
    return success(result)


def _pursuit_record(pursuit: Pursuit) -> dict[str, Any]:
    return {
        "pursuit_id": pursuit.id,
        "opportunity_id": pursuit.opportunity_id,
        "stage": pursuit.stage,
        "quote_price": pursuit.quote_price,
        "sourcing_cost": pursuit.sourcing_cost,
        "margin_pct": pursuit.margin_pct,
        "supplier": pursuit.supplier,
        "notes": pursuit.notes,
        "version": pursuit.version,
    }


def _assignment_record(row: Any) -> dict[str, Any]:
    return {
        "assignment_id": row.id,
        "opportunity_id": row.opportunity_id,
        "user_id": row.user_id,
        "assignment_role": row.assignment_role,
        "status": row.status,
        "recommendation": row.recommendation,
        "completed_at": row.completed_at,
    }


def _comment_record(row: ReviewComment) -> dict[str, Any]:
    return {
        "comment_id": row.id,
        "opportunity_id": row.opportunity_id,
        "user_id": row.user_id,
        "topic": row.topic,
        "body": row.body,
        "ai_position": row.ai_position,
        "ai_confidence": row.ai_confidence,
        "ai_reason": truncate_text(row.ai_reason, limit=500),
        "ai_missing_information": row.ai_missing_information,
    }


def _requirement_record(requirement: Requirement) -> dict[str, Any]:
    return {
        "requirement_id": requirement.id,
        "opportunity_id": requirement.opportunity_id,
        "status": requirement.status,
        "status_reason": requirement.status_reason,
        "version": requirement.version,
        "blocks_submission": requirement.blocks_submission,
    }
