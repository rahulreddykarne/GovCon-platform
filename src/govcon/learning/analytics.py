"""Outcome analytics — descriptive history only, no invented causal claims.

Phase 15 implementation per MASTER_SPEC_v2.5.md §21.
All analytics are descriptive; small samples are labeled; no causal overstatement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

from sqlalchemy import case, desc, func, select
from sqlalchemy.orm import Session

from govcon.models import Opportunity, OutcomeFeedback, Pursuit

logger = logging.getLogger("govcon.learning.analytics")

# Minimum wins required before a win profile is built or win-pattern claims are made
WIN_PROFILE_MINIMUM = 3

# Minimum group size to report a rate without a small-sample warning
SMALL_SAMPLE_THRESHOLD = 3


def current_outcome_ids():
    """Defend analytics against legacy duplicates until migration is applied."""
    return select(func.max(OutcomeFeedback.id)).group_by(OutcomeFeedback.opportunity_id)


def current_outcomes():
    return OutcomeFeedback.id.in_(current_outcome_ids())


@dataclass
class WinRateRow:
    key: str
    submitted: int
    won: int
    lost: int
    win_rate_pct: float | None
    small_sample: bool


@dataclass
class ReasonCount:
    reason: str
    count: int


@dataclass
class SupplierRow:
    supplier: str
    wins: int
    total: int
    win_rate_pct: float | None


@dataclass
class OutcomeAnalytics:
    total_submitted: int
    total_won: int
    total_lost: int
    total_no_bid: int
    overall_win_rate_pct: float | None
    avg_margin_pct_on_wins: float | None
    win_profile_available: bool
    win_profile_note: str

    by_psc: list[WinRateRow]
    by_agency: list[WinRateRow]
    by_size_bucket: list[WinRateRow]

    no_bid_reasons: list[ReasonCount]
    loss_reasons: list[ReasonCount]
    common_competitors: list[ReasonCount]
    reliable_suppliers: list[SupplierRow]

    avg_days_discovery_to_submission: float | None
    avg_amendment_count: float | None

    recent_outcomes: list[dict[str, Any]] = field(default_factory=list)


def _size_bucket(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 25_000:
        return "<$25K"
    if value < 100_000:
        return "$25K–$100K"
    if value < 500_000:
        return "$100K–$500K"
    if value < 2_000_000:
        return "$500K–$2M"
    return "$2M+"


def _win_rate_rows(
    session: Session,
    group_col: Any,
    limit: int = 20,
) -> list[tuple[str | None, int, int]]:
    """Return (group_key, total, won_count) tuples for submitted outcomes grouped by a column."""
    rows = session.execute(
        select(
            group_col,
            func.count(func.distinct(OutcomeFeedback.opportunity_id)).label("total"),
            func.count(func.distinct(case((OutcomeFeedback.outcome == "won", OutcomeFeedback.opportunity_id)))).label("won_count"),
        )
        .where(
            current_outcomes(),
            OutcomeFeedback.outcome.in_(["won", "lost"]),
            group_col.is_not(None),
        )
        .group_by(group_col)
        .order_by(desc("total"))
        .limit(limit)
    ).all()
    return [(key, total, int(won_count or 0)) for key, total, won_count in rows]


def win_rate_by_psc(session: Session, limit: int = 20) -> list[WinRateRow]:
    """Win/loss counts grouped by PSC code (from denorm_psc)."""
    result = []
    for psc, total, won_count in _win_rate_rows(session, OutcomeFeedback.denorm_psc, limit=limit):
        lost_count = total - won_count
        rate = (won_count / total * 100) if total > 0 else None
        result.append(
            WinRateRow(
                key=psc or "—",
                submitted=total,
                won=won_count,
                lost=lost_count,
                win_rate_pct=round(rate, 1) if rate is not None else None,
                small_sample=total < SMALL_SAMPLE_THRESHOLD,
            )
        )
    return result


def win_rate_by_agency(session: Session, limit: int = 20) -> list[WinRateRow]:
    """Win/loss counts grouped by agency (from denorm_agency)."""
    result = []
    for agency, total, won_count in _win_rate_rows(session, OutcomeFeedback.denorm_agency, limit=limit):
        rate = (won_count / total * 100) if total > 0 else None
        result.append(
            WinRateRow(
                key=agency or "—",
                submitted=total,
                won=won_count,
                lost=total - won_count,
                win_rate_pct=round(rate, 1) if rate is not None else None,
                small_sample=total < SMALL_SAMPLE_THRESHOLD,
            )
        )
    return result


def win_rate_by_size(session: Session) -> list[WinRateRow]:
    """Win/loss counts grouped by estimated contract value bucket."""
    rows = session.scalars(
        select(OutcomeFeedback).where(current_outcomes(), OutcomeFeedback.outcome.in_(["won", "lost"]))
    ).all()
    buckets: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = _size_bucket(
            float(row.denorm_estimated_value) if row.denorm_estimated_value is not None else None
        )
        if bucket not in buckets:
            buckets[bucket] = {"total": 0, "won": 0}
        buckets[bucket]["total"] += 1
        if row.outcome == "won":
            buckets[bucket]["won"] += 1

    order = ["<$25K", "$25K–$100K", "$100K–$500K", "$500K–$2M", "$2M+", "unknown"]
    result = []
    for key in order:
        if key not in buckets:
            continue
        total = buckets[key]["total"]
        won = buckets[key]["won"]
        rate = (won / total * 100) if total > 0 else None
        result.append(
            WinRateRow(
                key=key,
                submitted=total,
                won=won,
                lost=total - won,
                win_rate_pct=round(rate, 1) if rate is not None else None,
                small_sample=total < SMALL_SAMPLE_THRESHOLD,
            )
        )
    return result


def avg_margin_on_wins(session: Session) -> float | None:
    """Average win_margin_pct across won outcomes. None if no data."""
    val = session.scalar(
        select(func.avg(OutcomeFeedback.win_margin_pct)).where(
            current_outcomes(),
            OutcomeFeedback.outcome == "won",
            OutcomeFeedback.win_margin_pct.is_not(None),
        )
    )
    return float(round(val, 1)) if val is not None else None


def no_bid_reason_counts(session: Session) -> list[ReasonCount]:
    """Count structured no-bid categories."""
    rows = session.execute(
        select(
            func.coalesce(OutcomeFeedback.no_bid_category, OutcomeFeedback.no_bid_reason, "unspecified").label("reason"),
            func.count(func.distinct(OutcomeFeedback.opportunity_id)).label("cnt"),
        )
        .where(current_outcomes(), OutcomeFeedback.outcome == "no_bid")
        .group_by("reason")
        .order_by(desc("cnt"))
    ).all()
    return [ReasonCount(reason=r, count=c) for r, c in rows]


def loss_reason_counts(session: Session) -> list[ReasonCount]:
    """Count structured loss reason categories."""
    rows = session.execute(
        select(
            func.coalesce(OutcomeFeedback.loss_reason, "unspecified").label("reason"),
            func.count(func.distinct(OutcomeFeedback.opportunity_id)).label("cnt"),
        )
        .where(current_outcomes(), OutcomeFeedback.outcome == "lost")
        .group_by("reason")
        .order_by(desc("cnt"))
    ).all()
    return [ReasonCount(reason=r, count=c) for r, c in rows]


def common_competitors(session: Session, limit: int = 10) -> list[ReasonCount]:
    """Most frequent awarded vendors (competitors we lost to)."""
    rows = session.execute(
        select(
            OutcomeFeedback.awarded_vendor_name,
            func.count(func.distinct(OutcomeFeedback.opportunity_id)).label("cnt"),
        )
        .where(
            current_outcomes(),
            OutcomeFeedback.outcome == "lost",
            OutcomeFeedback.awarded_vendor_name.is_not(None),
        )
        .group_by(OutcomeFeedback.awarded_vendor_name)
        .order_by(desc("cnt"))
        .limit(limit)
    ).all()
    counted: list[ReasonCount] = []
    for name, count in rows:
        if not isinstance(name, str):
            continue
        counted.append(ReasonCount(reason=name, count=count))
    return counted


def reliable_suppliers(session: Session, limit: int = 10) -> list[SupplierRow]:
    """Wins / resolved submitted bids involving each sourcing supplier."""
    supplier = func.coalesce(OutcomeFeedback.win_supplier, Pursuit.supplier)
    rows = session.execute(
        select(
            supplier,
            func.count(func.distinct(case((OutcomeFeedback.outcome == "won", OutcomeFeedback.opportunity_id)))).label("wins"),
            func.count(func.distinct(OutcomeFeedback.opportunity_id)).label("total"),
        )
        .outerjoin(Pursuit, Pursuit.opportunity_id == OutcomeFeedback.opportunity_id)
        .where(
            current_outcomes(),
            OutcomeFeedback.outcome.in_(["won", "lost"]),
            supplier.is_not(None),
        )
        .group_by(supplier)
        .order_by(desc("wins"))
        .limit(limit)
    ).all()
    return [
        SupplierRow(
            supplier=s,
            wins=w,
            total=t,
            win_rate_pct=round(w / t * 100, 1) if t else None,
        )
        for s, w, t in rows
    ]


def avg_cycle_times(session: Session) -> dict[str, float | None]:
    """Average days from discovery (posted_date) to submission and average amendment count.

    Joins pursuits with opportunities to compute cycle times.
    Returns None for each metric when insufficient data exists.
    """
    from datetime import datetime

    pursuits = session.execute(
        select(Pursuit, Opportunity)
        .join(Opportunity, Pursuit.opportunity_id == Opportunity.id)
        .where(
            Pursuit.submitted_at.is_not(None),
        )
    ).all()
    cycle_days = []
    for pursuit, opp in pursuits:
        try:
            posted = opp.posted_date
            submitted = pursuit.submitted_at
            if posted is None or submitted is None:
                continue
            if isinstance(posted, datetime):
                posted_dt = posted if posted.tzinfo is not None else posted.replace(tzinfo=UTC)
            else:
                posted_dt = datetime(posted.year, posted.month, posted.day, tzinfo=UTC)
            if submitted.tzinfo is None:
                submitted = submitted.replace(tzinfo=UTC)
            delta = submitted - posted_dt
            cycle_days.append(delta.days)
        except Exception as exc:  # noqa: BLE001  boundary must record any failure
            logger.warning("skipped a pursuit while measuring cycle time: %s", exc)
            continue
    avg_days = (sum(cycle_days) / len(cycle_days)) if cycle_days else None
    from govcon.compliance.inventory import load_inventory
    amendment_counts = []
    for _, opp in pursuits:
        docs = load_inventory(session, opp).documents
        amendment_counts.append(len({d.amendment_number if d.amendment_number is not None else (d.sha256 or d.file_id) for d in docs if d.document_type == "amendment"}))
    return {
        "avg_days_discovery_to_submission": round(avg_days, 1) if avg_days is not None else None,
        "avg_amendment_count": round(sum(amendment_counts) / len(amendment_counts), 1) if amendment_counts else None,
    }


def win_count(session: Session) -> int:
    """Total number of recorded won outcomes."""
    return session.scalar(
        select(func.count(func.distinct(OutcomeFeedback.opportunity_id))).where(
            current_outcomes(),
            OutcomeFeedback.outcome == "won",
        )
    ) or 0


def win_profile_note(session: Session) -> tuple[bool, str]:
    """Return (available, note) for the win profile recommendation guard."""
    count = win_count(session)
    if count < WIN_PROFILE_MINIMUM:
        return (
            False,
            (
                f"Win profile not available: {count} win(s) recorded "
                f"(minimum {WIN_PROFILE_MINIMUM} required). "
                "Small-sample win patterns are not shown to avoid misleading recommendations."
            ),
        )
    return (True, f"Win profile based on {count} wins.")


def similar_past_outcomes(
    session: Session,
    opportunity_id: int,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return past outcomes from opportunities with the same PSC and/or agency.

    Used by decision reports to reference similar historical bids.
    Returns descriptive records only — no invented causal claims.
    """
    target = session.get(Opportunity, opportunity_id)
    if target is None:
        return []

    stmt = select(OutcomeFeedback, Opportunity).join(
        Opportunity, OutcomeFeedback.opportunity_id == Opportunity.id
    )
    filters = []
    if target.psc_code:
        filters.append(OutcomeFeedback.denorm_psc == target.psc_code)
    if target.agency_path:
        filters.append(OutcomeFeedback.denorm_agency == target.agency_path)

    if not filters:
        return []

    from sqlalchemy import or_

    stmt = (
        stmt.where(
            current_outcomes(),
            or_(*filters),
            OutcomeFeedback.opportunity_id != opportunity_id,
        )
        .order_by(desc(OutcomeFeedback.updated_at), desc(OutcomeFeedback.id))
        .limit(limit)
    )
    rows = session.execute(stmt).all()
    results = []
    for fb, opp in rows:
        results.append(
            {
                "opportunity_id": opp.id,
                "title": opp.title,
                "solicitation_number": opp.solicitation_number,
                "outcome": fb.outcome,
                "outcome_at": fb.updated_at,
                "award_amount": float(fb.award_amount) if fb.award_amount is not None else None,
                "loss_reason": fb.loss_reason,
                "win_reason": fb.win_reason,
                "no_bid_reason": fb.no_bid_reason,
                "lessons_learned": fb.lessons_learned,
                "match_reason": "same PSC" if fb.denorm_psc == target.psc_code else "same agency",
            }
        )
    return results


def outcome_analytics(session: Session) -> OutcomeAnalytics:
    """Full analytics snapshot — called by the learning page and MCP summary."""
    all_feedback = session.scalars(select(OutcomeFeedback).where(current_outcomes())).all()

    total_won = sum(1 for f in all_feedback if f.outcome == "won")
    total_lost = sum(1 for f in all_feedback if f.outcome == "lost")
    total_no_bid = sum(1 for f in all_feedback if f.outcome == "no_bid")
    total_submitted = total_won + total_lost

    overall_rate = (total_won / total_submitted * 100) if total_submitted > 0 else None
    avg_margin = avg_margin_on_wins(session)

    available, note = win_profile_note(session)
    times = avg_cycle_times(session)

    # Recent outcomes (last 20 terminal outcomes)
    recent = sorted(
        [f for f in all_feedback if f.outcome in ("won", "lost", "no_bid", "cancelled")],
        key=lambda f: f.updated_at,
        reverse=True,
    )[:20]
    opp_ids = list({f.opportunity_id for f in recent})
    opps = {}
    if opp_ids:
        opps = {
            o.id: o
            for o in session.scalars(
                select(Opportunity).where(Opportunity.id.in_(opp_ids))
            ).all()
        }
    recent_outcomes = []
    for fb in recent:
        opp = opps.get(fb.opportunity_id)
        recent_outcomes.append(
            {
                "opp_id": fb.opportunity_id,
                "title": opp.title if opp else None,
                "source_id": opp.source_id if opp else None,
                "outcome": fb.outcome,
                "outcome_at": fb.updated_at,
                "no_bid_reason": fb.no_bid_reason,
                "no_bid_category": fb.no_bid_category,
                "loss_reason": fb.loss_reason,
                "win_reason": fb.win_reason,
                "award_amount": float(fb.award_amount) if fb.award_amount is not None else None,
                "win_margin_pct": float(fb.win_margin_pct) if fb.win_margin_pct is not None else None,
                "lessons_learned": fb.lessons_learned,
            }
        )

    return OutcomeAnalytics(
        total_submitted=total_submitted,
        total_won=total_won,
        total_lost=total_lost,
        total_no_bid=total_no_bid,
        overall_win_rate_pct=round(overall_rate, 1) if overall_rate is not None else None,
        avg_margin_pct_on_wins=avg_margin,
        win_profile_available=available,
        win_profile_note=note,
        by_psc=win_rate_by_psc(session),
        by_agency=win_rate_by_agency(session),
        by_size_bucket=win_rate_by_size(session),
        no_bid_reasons=no_bid_reason_counts(session),
        loss_reasons=loss_reason_counts(session),
        common_competitors=common_competitors(session),
        reliable_suppliers=reliable_suppliers(session),
        avg_days_discovery_to_submission=times["avg_days_discovery_to_submission"],
        avg_amendment_count=times["avg_amendment_count"],
        recent_outcomes=recent_outcomes,
    )
