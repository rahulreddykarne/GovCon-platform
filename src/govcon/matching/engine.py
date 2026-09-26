"""Watchlist matching engine (Phase 2)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.matching.dedup import upsert_match
from govcon.matching.rules import evaluate_watchlist
from govcon.models import Match, Opportunity, Watchlist


@dataclass
class MatchRunStats:
    watchlists_evaluated: int = 0
    opportunities_scanned: int = 0
    matches_created: int = 0
    matches_updated: int = 0
    matches_removed: int = 0


def _active_opportunities(session: Session) -> list[Opportunity]:
    return list(
        session.scalars(
            select(Opportunity).where(Opportunity.status != "archived").order_by(Opportunity.id)
        ).all()
    )


def _enabled_watchlists(session: Session, watchlist_id: int | None = None) -> list[Watchlist]:
    query = select(Watchlist).where(Watchlist.enabled.is_(True)).order_by(Watchlist.id)
    if watchlist_id is not None:
        query = query.where(Watchlist.id == watchlist_id)
    return list(session.scalars(query).all())


def run_matching(
    session: Session,
    *,
    watchlist_id: int | None = None,
    rebuild: bool = False,
    now: datetime | None = None,
) -> MatchRunStats:
    """Evaluate enabled watchlists against active opportunities."""
    now = now or datetime.now(UTC)
    stats = MatchRunStats()
    watchlists = _enabled_watchlists(session, watchlist_id)
    if watchlist_id is not None and not watchlists:
        raise ValueError(f"watchlist {watchlist_id} not found or disabled")

    opportunities = _active_opportunities(session)
    stats.opportunities_scanned = len(opportunities)

    for watchlist in watchlists:
        stats.watchlists_evaluated += 1
        matched_ids: set[int] = set()
        for opportunity in opportunities:
            result = evaluate_watchlist(opportunity, watchlist, now=now)
            if not result.matches:
                continue
            matched_ids.add(opportunity.id)
            row, created = upsert_match(
                session,
                opportunity_id=opportunity.id,
                watchlist_id=watchlist.id,
                score=result.score,
                matched_on=result.matched_on,
            )
            if created:
                stats.matches_created += 1
            else:
                stats.matches_updated += 1

        watchlist.last_evaluated_at = now
        if rebuild:
            stale_query = select(Match).where(Match.watchlist_id == watchlist.id)
            if matched_ids:
                stale_query = stale_query.where(Match.opportunity_id.not_in(matched_ids))
            for match in session.scalars(stale_query).all():
                session.delete(match)
                stats.matches_removed += 1

    session.flush()
    return stats


def rebuild_watchlist(session: Session, watchlist_id: int, *, now: datetime | None = None) -> MatchRunStats:
    """Recompute matches for one watchlist and remove stale rows."""
    return run_matching(session, watchlist_id=watchlist_id, rebuild=True, now=now)
