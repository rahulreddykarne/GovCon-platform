"""Idempotent match upsert keyed by (opportunity_id, watchlist_id)."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from govcon.models import Match


def upsert_match(
    session: Session,
    *,
    opportunity_id: int,
    watchlist_id: int,
    score: Decimal,
    matched_on: dict,
) -> tuple[Match, bool]:
    """Insert or update a match row. Returns ``(row, created)``."""
    existing = session.scalar(
        select(Match).where(
            Match.opportunity_id == opportunity_id,
            Match.watchlist_id == watchlist_id,
        )
    )
    if existing is None:
        row = Match(
            opportunity_id=opportunity_id,
            watchlist_id=watchlist_id,
            score=score,
            matched_on=matched_on,
            status="new",
        )
        session.add(row)
        session.flush()
        return row, True

    existing.score = score
    existing.matched_on = matched_on
    flag_modified(existing, "matched_on")
    session.flush()
    return existing, False
