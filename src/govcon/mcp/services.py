"""Thin query/write helpers used by MCP tools.

These expose existing tables and patterns without new business rules.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from govcon.audit import record_audit
from govcon.concurrency import StaleRecordError, apply_versioned_update
from govcon.mcp.serialize import truncate_text
from govcon.models import (
    BidDecision,
    Match,
    Opportunity,
    OpportunityEvent,
    OpportunitySnapshot,
    Pursuit,
    User,
    Watchlist,
)

MATCH_STATUSES = frozenset({"new", "seen", "dismissed", "reviewing", "pursuing"})
DESTRUCTIVE_MATCH_STATUSES = frozenset({"dismissed"})
PURSUIT_STAGES = frozenset(
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
DESTRUCTIVE_PURSUIT_STAGES = frozenset({"won", "lost", "cancelled", "no_bid"})
HUMAN_BID_DECISIONS = frozenset({"approve_bid", "no_bid", "defer"})


def resolve_actor(session: Session, actor_email: str) -> User:
    email = actor_email.strip().lower()
    user = session.scalar(select(User).where(User.email == email, User.is_active.is_(True)))
    if user is None:
        raise ValueError(f"no active user {actor_email}")
    return user


def _opp_compact(row: Opportunity, *, include_full_description: bool = False) -> dict[str, Any]:
    description = row.description
    if not include_full_description:
        description = truncate_text(description)
    return {
        "id": row.id,
        "source": row.source,
        "source_id": row.source_id,
        "solicitation_number": row.solicitation_number,
        "title": row.title,
        "description": description,
        "opportunity_type": row.opportunity_type,
        "psc_code": row.psc_code,
        "naics_code": row.naics_code,
        "set_aside_code": row.set_aside_code,
        "agency_path": row.agency_path,
        "nsn": row.nsn,
        "quantity": row.quantity,
        "unit": row.unit,
        "estimated_value_min": row.estimated_value_min,
        "estimated_value_max": row.estimated_value_max,
        "posted_date": row.posted_date,
        "response_deadline": row.response_deadline,
        "status": row.status,
        "links": row.links,
    }


def search_opportunities(
    session: Session,
    *,
    query: str | None = None,
    source: str | None = None,
    status: str | None = None,
    psc_prefix: str | None = None,
    naics_prefix: str | None = None,
    nsn: str | None = None,
    closing_within_days: int | None = None,
    limit: int = 25,
    include_full_description: bool = False,
) -> list[dict[str, Any]]:
    statement: Select[tuple[Opportunity]] = select(Opportunity)
    if query:
        statement = statement.where(
            or_(
                Opportunity.title.ilike(f"%{query}%"),
                Opportunity.description.ilike(f"%{query}%"),
                Opportunity.solicitation_number.ilike(f"%{query}%"),
                Opportunity.source_id.ilike(f"%{query}%"),
            )
        )
    if source:
        statement = statement.where(Opportunity.source == source)
    if status:
        statement = statement.where(Opportunity.status == status)
    if psc_prefix:
        statement = statement.where(func.upper(Opportunity.psc_code).like(psc_prefix.upper() + "%"))
    if naics_prefix:
        statement = statement.where(func.upper(Opportunity.naics_code).like(naics_prefix.upper() + "%"))
    if nsn:
        statement = statement.where(Opportunity.nsn == nsn)
    if closing_within_days is not None:
        now = datetime.now(UTC)
        end = now + timedelta(days=closing_within_days)
        statement = statement.where(
            Opportunity.response_deadline.is_not(None),
            Opportunity.response_deadline >= now,
            Opportunity.response_deadline <= end,
        )
    statement = statement.order_by(Opportunity.response_deadline.asc().nulls_last(), Opportunity.id.desc())
    statement = statement.limit(max(1, min(limit, 100)))
    rows = list(session.scalars(statement).all())
    return [_opp_compact(row, include_full_description=include_full_description) for row in rows]


def get_opportunity(
    session: Session,
    opportunity_id: int,
    *,
    include_full_description: bool = False,
) -> dict[str, Any] | None:
    row = session.get(Opportunity, opportunity_id)
    if row is None:
        return None
    return _opp_compact(row, include_full_description=include_full_description)


def get_opportunity_history(session: Session, opportunity_id: int, *, limit: int = 50) -> dict[str, Any] | None:
    if session.get(Opportunity, opportunity_id) is None:
        return None
    snapshots = list(
        session.scalars(
            select(OpportunitySnapshot)
            .where(OpportunitySnapshot.opportunity_id == opportunity_id)
            .order_by(OpportunitySnapshot.fetched_at.desc(), OpportunitySnapshot.id.desc())
            .limit(max(1, min(limit, 100)))
        ).all()
    )
    events = list(
        session.scalars(
            select(OpportunityEvent)
            .where(OpportunityEvent.opportunity_id == opportunity_id)
            .order_by(OpportunityEvent.detected_at.desc(), OpportunityEvent.id.desc())
            .limit(max(1, min(limit, 100)))
        ).all()
    )
    return {
        "opportunity_id": opportunity_id,
        "snapshots": [
            {
                "id": snap.id,
                "fetched_at": snap.fetched_at,
                "content_hash": snap.content_hash,
                "source_version": snap.source_version,
            }
            for snap in snapshots
        ],
        "events": [
            {
                "id": event.id,
                "event_type": event.event_type,
                "field_name": event.field_name,
                "old_value": event.old_value,
                "new_value": event.new_value,
                "snapshot_id": event.snapshot_id,
                "detected_at": event.detected_at,
            }
            for event in events
        ],
    }


def list_matches(
    session: Session,
    *,
    status: str | None = None,
    watchlist_id: int | None = None,
    closing_within_days: int | None = None,
    limit: int = 50,
    include_full_description: bool = False,
) -> list[dict[str, Any]]:
    statement = (
        select(Match, Opportunity, Watchlist)
        .join(Opportunity, Opportunity.id == Match.opportunity_id)
        .join(Watchlist, Watchlist.id == Match.watchlist_id)
    )
    if status:
        statement = statement.where(Match.status == status)
    if watchlist_id is not None:
        statement = statement.where(Match.watchlist_id == watchlist_id)
    if closing_within_days is not None:
        now = datetime.now(UTC)
        end = now + timedelta(days=closing_within_days)
        statement = statement.where(
            Opportunity.response_deadline.is_not(None),
            Opportunity.response_deadline >= now,
            Opportunity.response_deadline <= end,
        )
    statement = statement.order_by(Opportunity.response_deadline.asc().nulls_last(), Match.id.desc())
    statement = statement.limit(max(1, min(limit, 100)))
    rows = []
    for match, opportunity, watchlist in session.execute(statement):
        rows.append(
            {
                "match_id": match.id,
                "status": match.status,
                "score": match.score,
                "matched_on": match.matched_on,
                "alerted_at": match.alerted_at,
                "watchlist": {"id": watchlist.id, "name": watchlist.name},
                "opportunity": _opp_compact(opportunity, include_full_description=include_full_description),
            }
        )
    return rows


def update_match(
    session: Session,
    *,
    match_id: int,
    status: str,
    actor: User,
    confirm: bool = False,
) -> dict[str, Any]:
    if status not in MATCH_STATUSES:
        raise ValueError(f"status must be one of {sorted(MATCH_STATUSES)}")
    if status in DESTRUCTIVE_MATCH_STATUSES and not confirm:
        raise ValueError("destructive match status requires confirm=true")
    match = session.get(Match, match_id)
    if match is None:
        raise ValueError(f"match not found: {match_id}")
    old_status = match.status
    match.status = status
    session.flush()
    record_audit(
        session,
        action_type="match_status_updated",
        user_id=actor.id,
        opportunity_id=match.opportunity_id,
        entity_type="matches",
        entity_id=match.id,
        old_value={"status": old_status},
        new_value={"status": status},
    )
    return {
        "match_id": match.id,
        "opportunity_id": match.opportunity_id,
        "watchlist_id": match.watchlist_id,
        "status": match.status,
        "score": match.score,
        "matched_on": match.matched_on,
    }


def pipeline_summary(session: Session) -> dict[str, Any]:
    counts = {
        stage: int(count)
        for stage, count in session.execute(
            select(Pursuit.stage, func.count()).group_by(Pursuit.stage)
        )
    }
    recent = list(
        session.execute(
            select(Pursuit, Opportunity)
            .join(Opportunity, Opportunity.id == Pursuit.opportunity_id)
            .order_by(Pursuit.updated_at.desc(), Pursuit.id.desc())
            .limit(25)
        )
    )
    return {
        "stage_counts": {stage: counts.get(stage, 0) for stage in sorted(PURSUIT_STAGES)},
        "total": sum(counts.values()),
        "recent": [
            {
                "pursuit_id": pursuit.id,
                "opportunity_id": pursuit.opportunity_id,
                "stage": pursuit.stage,
                "title": opportunity.title,
                "response_deadline": opportunity.response_deadline,
                "sourcing_cost": pursuit.sourcing_cost,
                "quote_price": pursuit.quote_price,
                "margin_pct": pursuit.margin_pct,
                "version": pursuit.version,
            }
            for pursuit, opportunity in recent
        ],
    }


def add_pursuit(
    session: Session,
    *,
    opportunity_id: int,
    actor: User,
    stage: str = "evaluating",
    notes: str | None = None,
    sourcing_cost: Decimal | None = None,
    quote_price: Decimal | None = None,
    supplier: str | None = None,
) -> dict[str, Any]:
    if stage not in PURSUIT_STAGES:
        raise ValueError(f"stage must be one of {sorted(PURSUIT_STAGES)}")
    if session.get(Opportunity, opportunity_id) is None:
        raise ValueError(f"opportunity not found: {opportunity_id}")
    existing = session.scalar(select(Pursuit).where(Pursuit.opportunity_id == opportunity_id))
    if existing is not None:
        raise ValueError(f"pursuit already exists for opportunity {opportunity_id}")
    pursuit = Pursuit(
        opportunity_id=opportunity_id,
        stage=stage,
        notes=notes,
        sourcing_cost=sourcing_cost,
        quote_price=quote_price,
        supplier=supplier,
    )
    session.add(pursuit)
    session.flush()
    record_audit(
        session,
        action_type="pursuit_created",
        user_id=actor.id,
        opportunity_id=opportunity_id,
        entity_type="pursuits",
        entity_id=pursuit.id,
        new_value={"stage": stage, "notes": notes},
    )
    return _pursuit_record(pursuit)


def update_pursuit(
    session: Session,
    *,
    pursuit_id: int,
    actor: User,
    expected_version: int,
    stage: str | None = None,
    notes: str | None = None,
    sourcing_cost: Decimal | None = None,
    quote_price: Decimal | None = None,
    supplier: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    pursuit = session.get(Pursuit, pursuit_id)
    if pursuit is None:
        raise ValueError(f"pursuit not found: {pursuit_id}")
    changes: dict[str, Any] = {}
    if stage is not None:
        if stage not in PURSUIT_STAGES:
            raise ValueError(f"stage must be one of {sorted(PURSUIT_STAGES)}")
        if stage in DESTRUCTIVE_PURSUIT_STAGES and not confirm:
            raise ValueError("destructive pursuit stage requires confirm=true")
        changes["stage"] = stage
    if notes is not None:
        changes["notes"] = notes
    if sourcing_cost is not None:
        changes["sourcing_cost"] = sourcing_cost
    if quote_price is not None:
        changes["quote_price"] = quote_price
    if supplier is not None:
        changes["supplier"] = supplier
    if not changes:
        raise ValueError("no pursuit fields to update")
    old = {key: getattr(pursuit, key) for key in changes}
    try:
        apply_versioned_update(session, pursuit, expected_version, changes)
    except StaleRecordError as exc:
        raise ValueError(str(exc)) from exc
    record_audit(
        session,
        action_type="pursuit_updated",
        user_id=actor.id,
        opportunity_id=pursuit.opportunity_id,
        entity_type="pursuits",
        entity_id=pursuit.id,
        old_value=old,
        new_value=changes,
    )
    return _pursuit_record(pursuit)


def _pursuit_record(pursuit: Pursuit) -> dict[str, Any]:
    return {
        "pursuit_id": pursuit.id,
        "opportunity_id": pursuit.opportunity_id,
        "stage": pursuit.stage,
        "sourcing_cost": pursuit.sourcing_cost,
        "quote_price": pursuit.quote_price,
        "margin_pct": pursuit.margin_pct,
        "supplier": pursuit.supplier,
        "notes": pursuit.notes,
        "approved_to_bid_at": pursuit.approved_to_bid_at,
        "submitted_at": pursuit.submitted_at,
        "version": pursuit.version,
    }


def get_bid_analysis(session: Session, opportunity_id: int) -> dict[str, Any] | None:
    if session.get(Opportunity, opportunity_id) is None:
        return None
    decision = session.scalar(
        select(BidDecision)
        .where(BidDecision.opportunity_id == opportunity_id)
        .order_by(BidDecision.created_at.desc(), BidDecision.id.desc())
        .limit(1)
    )
    if decision is None:
        return {
            "opportunity_id": opportunity_id,
            "bid_decision": None,
            "message": "no bid decision recorded yet",
        }
    return {
        "opportunity_id": opportunity_id,
        "bid_decision": {
            "id": decision.id,
            "recommendation": decision.recommendation,
            "recommendation_score": decision.recommendation_score,
            "capability_score": decision.capability_score,
            "pricing_score": decision.pricing_score,
            "past_performance_score": decision.past_performance_score,
            "deadline_score": decision.deadline_score,
            "competition_score": decision.competition_score,
            "margin_score": decision.margin_score,
            "compliance_risk_score": decision.compliance_risk_score,
            "strengths": decision.strengths,
            "risks": decision.risks,
            "missing_information": decision.missing_information,
            "evidence": decision.evidence,
            "human_decision": decision.human_decision,
            "human_comments": decision.human_comments,
            "reviewed_at": decision.reviewed_at,
            "created_at": decision.created_at,
        },
    }


def record_human_bid_decision(
    session: Session,
    *,
    opportunity_id: int,
    actor: User,
    human_decision: str,
    human_comments: str | None = None,
    bid_decision_id: int | None = None,
) -> dict[str, Any]:
    if human_decision not in HUMAN_BID_DECISIONS:
        raise ValueError(f"human_decision must be one of {sorted(HUMAN_BID_DECISIONS)}")
    if bid_decision_id is not None:
        decision = session.get(BidDecision, bid_decision_id)
        if decision is None or decision.opportunity_id != opportunity_id:
            raise ValueError(f"bid decision not found for opportunity {opportunity_id}: {bid_decision_id}")
    else:
        decision = session.scalar(
            select(BidDecision)
            .where(BidDecision.opportunity_id == opportunity_id)
            .order_by(BidDecision.created_at.desc(), BidDecision.id.desc())
            .limit(1)
        )
        if decision is None:
            raise ValueError(f"no bid decision exists for opportunity {opportunity_id}")
    old = {
        "human_decision": decision.human_decision,
        "human_comments": decision.human_comments,
        "reviewed_at": decision.reviewed_at.isoformat() if decision.reviewed_at else None,
    }
    now = datetime.now(UTC)
    decision.human_decision = human_decision
    decision.human_comments = human_comments
    decision.reviewed_at = now
    session.flush()
    record_audit(
        session,
        action_type="bid_decision_human_recorded",
        user_id=actor.id,
        opportunity_id=opportunity_id,
        entity_type="bid_decisions",
        entity_id=decision.id,
        old_value=old,
        new_value={
            "human_decision": human_decision,
            "human_comments": human_comments,
            "reviewed_at": now.isoformat(),
        },
    )
    return {
        "bid_decision_id": decision.id,
        "opportunity_id": opportunity_id,
        "human_decision": decision.human_decision,
        "human_comments": decision.human_comments,
        "reviewed_at": decision.reviewed_at,
        "recommendation": decision.recommendation,
    }
