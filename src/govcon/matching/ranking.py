"""Explainable 0–100 ranking of active matches (roadmap gap 5, ADR-069).

Each factor is scored 0–1 from recorded data and shown with its weight and
its contribution in points. A factor with no data (no deadline, no value
estimate, no semantic run, fewer than three wins) is listed as unavailable
and the remaining weights are rescaled; unknown is never scored as good or
bad. ``matches.score`` stays the rule-hit count it has always been.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.matching.recommendations import active_similarity
from govcon.models import Match, Opportunity

# Weights approved by the user (roadmap Q4, 2026-10-03); they sum to 100.
WEIGHTS: dict[str, int] = {
    "rule_match": 30,
    "semantic_similarity": 20,
    "similar_to_wins": 15,
    "value": 15,
    "time_remaining": 10,
    "competition": 10,
}
LABELS = {
    "rule_match": "Watchlist rules matched",
    "semantic_similarity": "Similar to the watchlist profile",
    "similar_to_wins": "Similar to past wins",
    "value": "Estimated value",
    "time_remaining": "Time to respond",
    "competition": "Likely competitors",
}


def _rule_match(match: Match) -> tuple[float | None, str]:
    matched = match.matched_on or {}
    active = matched.get("active_groups") or []
    passing = matched.get("passing_groups") or []
    if not active:
        return None, "no active rule groups"
    return len(passing) / len(active), f"{len(passing)} of {len(active)} rule groups passed"


def _value(opportunity: Opportunity) -> tuple[float | None, str]:
    value = opportunity.estimated_value_max or opportunity.estimated_value_min
    if value is None or value <= 0:
        return None, "no value estimate"
    # Log scale: $10K scores 0, $10M or more scores 1.
    score = (math.log10(float(value)) - 4) / 3
    return max(0.0, min(1.0, score)), f"about ${float(value):,.0f}"


def _time_remaining(opportunity: Opportunity, now: datetime) -> tuple[float | None, str]:
    if opportunity.response_deadline is None:
        return None, "no response deadline"
    days = (opportunity.response_deadline - now).total_seconds() / 86400
    # Under 2 days scores 0; 21 days or more scores 1.
    return max(0.0, min(1.0, (days - 2) / 19)), f"{max(0, int(days))} days left"


def _competition(session: Session, opportunity: Opportunity, cache: dict[int, int | None]) -> tuple[float | None, str]:
    if opportunity.id not in cache:
        from govcon.intelligence.competitors import competitor_summary

        summary = competitor_summary(session, opportunity.id, limit=10)
        names: set[str] = set()
        for bucket in summary.buckets if summary else ():
            for winner in bucket.winners:
                names.add(winner.recipient_uei or winner.recipient_name or "")
        names.discard("")
        cache[opportunity.id] = len(names) or None
    count = cache[opportunity.id]
    if count is None:
        return None, "no award history"
    # One past awardee scores 1; ten or more score 0.
    return max(0.0, 1 - (count - 1) / 9), f"{count} distinct past awardee(s)"


def _similarity(value: Decimal | None, what: str) -> tuple[float | None, str]:
    if value is None:
        return None, f"no {what} recommendation"
    return float(value), f"similarity {float(value):.2f}"


def rank_match(session: Session, match: Match, opportunity: Opportunity, *, now: datetime,
               competition_cache: dict[int, int | None] | None = None) -> tuple[Decimal, list[dict[str, Any]]]:
    """Score one match and return ``(score 0-100, factors)``."""
    cache = competition_cache if competition_cache is not None else {}
    raw: dict[str, tuple[float | None, str]] = {
        "rule_match": _rule_match(match),
        "semantic_similarity": _similarity(active_similarity(
            session, opportunity_id=opportunity.id, category="semantic_match", watchlist_id=match.watchlist_id),
            "semantic"),
        "similar_to_wins": _similarity(active_similarity(
            session, opportunity_id=opportunity.id, category="similar_to_won"), "similar-to-wins"),
        "value": _value(opportunity),
        "time_remaining": _time_remaining(opportunity, now),
        "competition": _competition(session, opportunity, cache),
    }
    available = sum(WEIGHTS[name] for name, (value, _) in raw.items() if value is not None)
    factors: list[dict[str, Any]] = []
    total = 0.0
    for name, (value, evidence) in raw.items():
        weight = WEIGHTS[name]
        points = (weight / available * 100 * value) if value is not None and available else None
        total += points or 0.0
        factors.append({
            "name": name, "label": LABELS[name], "weight": weight, "available": value is not None,
            "raw": round(value, 4) if value is not None else None,
            "points": round(points, 1) if points is not None else None, "evidence": evidence,
        })
    return Decimal(str(round(total, 1))), factors


def rank_active_matches(session: Session, *, now: datetime | None = None) -> int:
    """Rank every active match; returns how many were ranked."""
    now = now or datetime.now(UTC)
    cache: dict[int, int | None] = {}
    count = 0
    for match, opportunity in session.execute(
        select(Match, Opportunity).join(Opportunity, Opportunity.id == Match.opportunity_id).where(Match.active.is_(True))
    ):
        match.rank_score, match.rank_factors = rank_match(session, match, opportunity, now=now, competition_cache=cache)
        match.ranked_at = now
        count += 1
    session.flush()
    return count
