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
from govcon.models import Match, Opportunity, Recommendation

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


def _bucket_names(session: Session, opportunity: Opportunity, buckets: dict[tuple[str, str], list[str]]) -> set[str]:
    """Distinct awardees, reusing one query per NSN, PSC, agency segment, or office."""
    from govcon.intelligence.awards import top_awardees
    from govcon.intelligence.competitors import (
        opportunity_office,
        top_awardees_for_agency,
        top_awardees_for_office,
    )
    from govcon.matching.pricing import canonical_nsn

    def labels(key: tuple[str, str], loader) -> list[str]:
        if key not in buckets:
            found: list[str] = []
            seen: set[str] = set()
            for winner in loader():
                label = winner.recipient_uei or winner.recipient_name or ""
                if label and label not in seen:
                    seen.add(label)
                    found.append(label)
            buckets[key] = found
        return buckets[key]

    names: set[str] = set()
    if opportunity.nsn:
        normalized = canonical_nsn(opportunity.nsn)
        if normalized is not None:
            names.update(labels(("nsn", normalized), lambda: top_awardees(session, nsn=normalized, limit=10)))
    if opportunity.psc_code:
        code = opportunity.psc_code
        names.update(labels(("psc", code), lambda: top_awardees(session, psc=code, limit=10)))
    if opportunity.agency_path:
        segments = [part.strip() for part in opportunity.agency_path.split(".") if part.strip()]
        agency_names: list[str] = []
        seen_agency: set[str] = set()
        for segment in reversed(segments):
            for label in labels(("agency", segment), lambda segment=segment: top_awardees_for_agency(session, segment, limit=10)):
                if label not in seen_agency:
                    seen_agency.add(label)
                    agency_names.append(label)
                if len(agency_names) >= 10:
                    break
            if len(agency_names) >= 10:
                break
        names.update(agency_names[:10])
    office = opportunity_office(opportunity)
    if office:
        names.update(labels(("office", office), lambda: top_awardees_for_office(session, office, limit=10)))
    return names


def _competition(
    session: Session,
    opportunity: Opportunity,
    cache: dict[int, int | None],
    buckets: dict[tuple[str, str], list[str]] | None = None,
) -> tuple[float | None, str]:
    if opportunity.id not in cache:
        names = _bucket_names(session, opportunity, buckets if buckets is not None else {})
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


def load_similarity_index(
    session: Session, opportunity_ids: list[int]
) -> dict[tuple[int, str, int | None], Decimal | None]:
    """Best active similarity for each opportunity, category, and watchlist."""
    if not opportunity_ids:
        return {}
    rows = session.execute(
        select(
            Recommendation.opportunity_id,
            Recommendation.category,
            Recommendation.watchlist_id,
            Recommendation.similarity,
        ).where(
            Recommendation.opportunity_id.in_(opportunity_ids),
            Recommendation.active.is_(True),
            Recommendation.category.in_(("semantic_match", "similar_to_won")),
        )
    )
    index: dict[tuple[int, str, int | None], Decimal | None] = {}
    for opportunity_id, category, watchlist_id, similarity in rows:
        key = (opportunity_id, category, watchlist_id if category == "semantic_match" else None)
        current = index.get(key)
        if key not in index or (similarity is not None and (current is None or similarity > current)):
            index[key] = similarity
    return index


def _indexed_similarity(
    session: Session,
    index: dict[tuple[int, str, int | None], Decimal | None] | None,
    *,
    opportunity_id: int,
    category: str,
    watchlist_id: int | None,
) -> Decimal | None:
    if index is None:
        return active_similarity(
            session, opportunity_id=opportunity_id, category=category,
            watchlist_id=watchlist_id if category == "semantic_match" else None,
        )
    key = (opportunity_id, category, watchlist_id if category == "semantic_match" else None)
    return index.get(key)


def rank_match(session: Session, match: Match, opportunity: Opportunity, *, now: datetime,
               competition_cache: dict[int, int | None] | None = None,
               similarity_index: dict[tuple[int, str, int | None], Decimal | None] | None = None,
               bucket_cache: dict[tuple[str, str], list[str]] | None = None) -> tuple[Decimal, list[dict[str, Any]]]:
    """Score one match and return ``(score 0-100, factors)``."""
    cache = competition_cache if competition_cache is not None else {}
    raw: dict[str, tuple[float | None, str]] = {
        "rule_match": _rule_match(match),
        "semantic_similarity": _similarity(_indexed_similarity(
            session, similarity_index, opportunity_id=opportunity.id, category="semantic_match",
            watchlist_id=match.watchlist_id), "semantic"),
        "similar_to_wins": _similarity(_indexed_similarity(
            session, similarity_index, opportunity_id=opportunity.id, category="similar_to_won",
            watchlist_id=None), "similar-to-wins"),
        "value": _value(opportunity),
        "time_remaining": _time_remaining(opportunity, now),
        "competition": _competition(session, opportunity, cache, bucket_cache),
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
    buckets: dict[tuple[str, str], list[str]] = {}
    pairs = session.execute(
        select(Match, Opportunity).join(Opportunity, Opportunity.id == Match.opportunity_id).where(Match.active.is_(True))
    ).all()
    index = load_similarity_index(session, list({opportunity.id for _, opportunity in pairs}))
    count = 0
    for match, opportunity in pairs:
        match.rank_score, match.rank_factors = rank_match(
            session, match, opportunity, now=now, competition_cache=cache,
            similarity_index=index, bucket_cache=buckets,
        )
        match.ranked_at = now
        count += 1
    session.flush()
    return count
