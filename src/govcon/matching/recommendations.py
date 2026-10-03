"""Persist semantic recommendations with their evidence and input revisions (ADR-069).

The scheduled semantic step used to compute recommendations and discard them.
Each run now upserts one row per (opportunity, watchlist, category), records
the embedding model version and the watchlist and opportunity text hashes it
was computed from, and deactivates rows the run no longer produces. Rows are
never deleted.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from govcon.matching.semantic import (
    CATEGORY_RECOMPETE_RADAR,
    CATEGORY_SEMANTIC_MATCH,
    CATEGORY_SIMILAR_PURSUED,
    CATEGORY_SIMILAR_WON,
    pursued_profile_recommendations,
    recompete_radar,
    semantic_recommendations_for_watchlist,
    win_profile_recommendations,
)
from govcon.models import Opportunity, Recommendation

CATEGORY_KEYS = {
    CATEGORY_SEMANTIC_MATCH: "semantic_match",
    CATEGORY_SIMILAR_WON: "similar_to_won",
    CATEGORY_SIMILAR_PURSUED: "similar_to_pursued",
    CATEGORY_RECOMPETE_RADAR: "recompete_radar",
}


@dataclass
class PersistStats:
    inserted: int = 0
    updated: int = 0
    deactivated: int = 0
    seconds: float = 0.0
    watchlists: int = 0


def _similarity(item: dict[str, Any]) -> Decimal | None:
    distance = item.get("cosine_distance")
    if distance is None:
        return None
    return Decimal(str(round(max(0.0, 1.0 - float(distance)), 4)))


def _upsert(session: Session, stats: PersistStats, seen: set[int], *, opportunity_id: int,
            watchlist_id: int | None, category: str, item: dict[str, Any], model_version: str | None,
            profile_hash: str | None, now: datetime) -> None:
    opp_hash = session.scalar(select(Opportunity.embedding_source_hash).where(Opportunity.id == opportunity_id))
    query = select(Recommendation).where(
        Recommendation.opportunity_id == opportunity_id,
        Recommendation.category == category,
        Recommendation.watchlist_id.is_(None) if watchlist_id is None else Recommendation.watchlist_id == watchlist_id,
    )
    row = session.scalar(query)
    values = {
        "similarity": _similarity(item), "evidence": item, "embedding_model_version": model_version,
        "watchlist_profile_hash": profile_hash, "opportunity_source_hash": opp_hash, "active": True,
        "last_seen_at": now,
    }
    if row is None:
        row = Recommendation(opportunity_id=opportunity_id, watchlist_id=watchlist_id, category=category, **values)
        session.add(row)
        session.flush()
        stats.inserted += 1
    else:
        for key, value in values.items():
            setattr(row, key, value)
        stats.updated += 1
    seen.add(row.id)


def refresh_recommendations(session: Session, provider, *, limit: int = 20, now: datetime | None = None) -> PersistStats:
    """Recompute every category for enabled watchlists and persist the results."""
    from govcon.matching.watchlists import list_watchlists

    started = time.monotonic()
    now = now or datetime.now(UTC)
    stats = PersistStats()
    seen: set[int] = set()
    try:
        model_version = provider.model_version
    except Exception:  # a provider that cannot report its version still persists results  # noqa: BLE001  boundary must record any failure
        model_version = None
    for watchlist in list_watchlists(session, include_disabled=False):
        stats.watchlists += 1
        result = semantic_recommendations_for_watchlist(session, watchlist.id, limit=limit)
        for item in result.get("matches", []):
            _upsert(session, stats, seen, opportunity_id=item["id"], watchlist_id=watchlist.id,
                    category=CATEGORY_KEYS[CATEGORY_SEMANTIC_MATCH], item=item, model_version=model_version,
                    profile_hash=watchlist.embedding_source_hash, now=now)
    for label, result in (
        (CATEGORY_SIMILAR_WON, win_profile_recommendations(session, provider, limit=limit)),
        (CATEGORY_SIMILAR_PURSUED, pursued_profile_recommendations(session, provider, limit=limit)),
        (CATEGORY_RECOMPETE_RADAR, recompete_radar(session, limit=limit)),
    ):
        for item in result.get("matches", []):
            _upsert(session, stats, seen, opportunity_id=item["id"], watchlist_id=None,
                    category=CATEGORY_KEYS[label], item=item, model_version=model_version,
                    profile_hash=None, now=now)
    stale = session.execute(
        update(Recommendation)
        .where(Recommendation.active.is_(True), Recommendation.id.not_in(seen or {-1}))
        .values(active=False)
    )
    stats.deactivated = cast(CursorResult[Any], stale).rowcount or 0
    session.flush()
    stats.seconds = round(time.monotonic() - started, 3)
    return stats


def active_similarity(session: Session, *, opportunity_id: int, category: str,
                      watchlist_id: int | None = None) -> Decimal | None:
    query = select(Recommendation.similarity).where(
        Recommendation.opportunity_id == opportunity_id, Recommendation.category == category,
        Recommendation.active.is_(True),
    )
    if watchlist_id is not None:
        query = query.where(Recommendation.watchlist_id == watchlist_id)
    return session.scalar(query.order_by(Recommendation.similarity.desc().nulls_last()).limit(1))
