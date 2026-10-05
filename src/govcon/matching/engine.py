"""Deterministic watchlist matching engine (Phase 2).

Every run reconciles: only open opportunities whose deadline has not passed
are evaluated, and a stored match the watchlist no longer produces (criteria
changed, opportunity closed, watchlist disabled) is marked inactive rather
than deleted, keeping its triage status and alert history. A match that
starts matching again is reactivated with that history intact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from govcon.matching.eligibility import ELIGIBLE_STATUSES, pursuit_eligibility
from govcon.matching.pricing import canonical_nsn
from govcon.models import Match, Opportunity, Watchlist

OPEN_STATUS = "open"
INACTIVE_NO_LONGER_MATCHES = "no_longer_matches"
INACTIVE_OPPORTUNITY_CLOSED = "opportunity_closed"
INACTIVE_WATCHLIST_DISABLED = "watchlist_disabled"
# Evidence that changes with the clock alone; ignored when deciding whether a
# stored match changed.
_TIME_VARYING_KEYS = frozenset({"days_remaining"})

GROUP_NAMES = (
    "psc",
    "naics",
    "keywords",
    "exclude_keywords",
    "nsn",
    "set_asides",
    "sources",
    "value",
    "deadline",
)


@dataclass
class MatchStats:
    evaluated: int = 0
    matched: int = 0
    inserted: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    reactivated: int = 0


def _non_empty(values: list[str] | None) -> list[str]:
    if not values:
        return []
    return [value for value in values if value]


def _search_text(opportunity: Opportunity) -> str:
    parts = [opportunity.title or "", opportunity.description or ""]
    return " ".join(parts).lower()


def _keyword_hits(keyword: str, haystack: str) -> bool:
    pattern = r"\b" + re.escape(keyword.lower()) + r"\b"
    return re.search(pattern, haystack) is not None


def _wildcard_evidence() -> dict[str, Any]:
    return {"status": "wildcard"}


def _evaluate_psc(watchlist: Watchlist, opportunity: Opportunity) -> dict[str, Any]:
    configured = _non_empty(watchlist.psc_codes)
    if not configured:
        return _wildcard_evidence()
    psc = opportunity.psc_code or ""
    matched_prefix = next((prefix for prefix in configured if psc.startswith(prefix)), None)
    return {
        "status": "pass" if matched_prefix else "fail",
        "configured": configured,
        "opportunity": psc or None,
        "matched_prefix": matched_prefix,
    }


def _evaluate_naics(watchlist: Watchlist, opportunity: Opportunity) -> dict[str, Any]:
    configured = _non_empty(watchlist.naics_codes)
    if not configured:
        return _wildcard_evidence()
    naics = opportunity.naics_code or ""
    matched_prefix = next((prefix for prefix in configured if naics.startswith(prefix)), None)
    return {
        "status": "pass" if matched_prefix else "fail",
        "configured": configured,
        "opportunity": naics or None,
        "matched_prefix": matched_prefix,
    }


def _evaluate_keywords(watchlist: Watchlist, opportunity: Opportunity) -> dict[str, Any]:
    configured = _non_empty(watchlist.keywords)
    if not configured:
        return _wildcard_evidence()
    haystack = _search_text(opportunity)
    matched = [keyword for keyword in configured if _keyword_hits(keyword, haystack)]
    return {
        "status": "pass" if matched else "fail",
        "configured": configured,
        "matched": matched,
    }


def _evaluate_exclude_keywords(watchlist: Watchlist, opportunity: Opportunity) -> dict[str, Any]:
    configured = _non_empty(watchlist.exclude_keywords)
    if not configured:
        return _wildcard_evidence()
    haystack = _search_text(opportunity)
    matched = [keyword for keyword in configured if _keyword_hits(keyword, haystack)]
    return {
        "status": "fail" if matched else "pass",
        "configured": configured,
        "matched": matched,
    }


def _nsn_key(value: str) -> str:
    """Dashed canonical form when the value is an NSN, else the trimmed text."""
    return canonical_nsn(value) or value.strip().upper()


def _evaluate_nsn(watchlist: Watchlist, opportunity: Opportunity) -> dict[str, Any]:
    configured = _non_empty(watchlist.nsn_list)
    if not configured:
        return _wildcard_evidence()
    wanted = {_nsn_key(value) for value in configured}
    candidates: list[str] = []
    for value in [*(getattr(opportunity, "nsn_candidates", None) or []), opportunity.nsn]:
        if value and _nsn_key(value) not in candidates:
            candidates.append(_nsn_key(value))
    matched = [candidate for candidate in candidates if candidate in wanted]
    return {
        "status": "pass" if matched else "fail",
        "configured": configured,
        "opportunity": opportunity.nsn or None,
        "candidates": candidates,
        "matched": matched,
    }


def _evaluate_set_asides(watchlist: Watchlist, opportunity: Opportunity) -> dict[str, Any]:
    configured = _non_empty(watchlist.set_asides)
    if not configured:
        return _wildcard_evidence()
    set_aside = opportunity.set_aside_code or ""
    matched = set_aside in configured
    return {
        "status": "pass" if matched else "fail",
        "configured": configured,
        "opportunity": set_aside or None,
    }


def _evaluate_sources(watchlist: Watchlist, opportunity: Opportunity) -> dict[str, Any]:
    configured = _non_empty(watchlist.sources)
    if not configured:
        return _wildcard_evidence()
    source = opportunity.source
    matched = source in configured
    return {
        "status": "pass" if matched else "fail",
        "configured": configured,
        "opportunity": source,
    }


def _evaluate_value(watchlist: Watchlist, opportunity: Opportunity) -> dict[str, Any]:
    min_configured = watchlist.min_value
    max_configured = watchlist.max_value
    if min_configured is None and max_configured is None:
        return _wildcard_evidence()

    opp_min = opportunity.estimated_value_min
    opp_max = opportunity.estimated_value_max
    evidence: dict[str, Any] = {
        "min_configured": str(min_configured) if min_configured is not None else None,
        "max_configured": str(max_configured) if max_configured is not None else None,
        "opportunity_min": str(opp_min) if opp_min is not None else None,
        "opportunity_max": str(opp_max) if opp_max is not None else None,
    }

    if opp_min is None and opp_max is None:
        evidence["status"] = "unknown"
        return evidence

    status = "pass"
    if min_configured is not None:
        comparable = opp_max if opp_max is not None else opp_min
        if comparable is not None and comparable < min_configured:
            status = "fail"
    if max_configured is not None and status != "fail":
        comparable = opp_min if opp_min is not None else opp_max
        if comparable is not None and comparable > max_configured:
            status = "fail"
    evidence["status"] = status
    return evidence


def _evaluate_deadline(
    watchlist: Watchlist,
    opportunity: Opportunity,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    min_days = watchlist.min_deadline_days
    if min_days is None:
        return _wildcard_evidence()

    deadline = opportunity.response_deadline
    if deadline is None:
        return {
            "status": "unknown",
            "min_days_configured": min_days,
            "days_remaining": None,
        }

    now = now or datetime.now(UTC)
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    days_remaining = (deadline - now).total_seconds() / 86400
    return {
        "status": "pass" if days_remaining >= min_days else "fail",
        "min_days_configured": min_days,
        "days_remaining": round(days_remaining, 2),
        "response_deadline": deadline.isoformat(),
    }


def evaluate_match(
    watchlist: Watchlist,
    opportunity: Opportunity,
    *,
    now: datetime | None = None,
) -> tuple[bool, dict[str, Any], Decimal]:
    """Return ``(is_match, matched_on, score)`` for one watchlist/opportunity pair."""
    groups = {
        "psc": _evaluate_psc(watchlist, opportunity),
        "naics": _evaluate_naics(watchlist, opportunity),
        "keywords": _evaluate_keywords(watchlist, opportunity),
        "exclude_keywords": _evaluate_exclude_keywords(watchlist, opportunity),
        "nsn": _evaluate_nsn(watchlist, opportunity),
        "set_asides": _evaluate_set_asides(watchlist, opportunity),
        "sources": _evaluate_sources(watchlist, opportunity),
        "value": _evaluate_value(watchlist, opportunity),
        "deadline": _evaluate_deadline(watchlist, opportunity, now=now),
    }

    active_groups = [name for name, evidence in groups.items() if evidence["status"] != "wildcard"]
    passing_groups = [
        name
        for name in active_groups
        if groups[name]["status"] in {"pass", "unknown"}
    ]
    is_match = len(active_groups) > 0 and len(passing_groups) == len(active_groups)

    score = Decimal(len(passing_groups))
    matched_on = {
        "groups": groups,
        "active_groups": active_groups,
        "passing_groups": passing_groups,
        "score_basis": "rule_hits",
    }
    return is_match, matched_on, score


def _is_open(opportunity: Opportunity, now: datetime) -> bool:
    return pursuit_eligibility(opportunity, now)[0] != "ineligible"


def _opportunity_query(watchlist: Watchlist, now: datetime):
    """Open opportunities whose response deadline has not passed (or is unknown)."""
    query = select(Opportunity).where(
        Opportunity.status.in_(ELIGIBLE_STATUSES),
        or_(Opportunity.response_deadline.is_(None), Opportunity.response_deadline > now),
    )
    configured_sources = _non_empty(watchlist.sources)
    if configured_sources:
        query = query.where(Opportunity.source.in_(configured_sources))
    return query


def _stable(value: Any) -> Any:
    """``matched_on`` without the clock-driven fields."""
    if isinstance(value, dict):
        return {key: _stable(item) for key, item in value.items() if key not in _TIME_VARYING_KEYS}
    if isinstance(value, list):
        return [_stable(item) for item in value]
    return value


def _upsert_match(
    session: Session,
    *,
    opportunity_id: int,
    watchlist_id: int,
    score: Decimal,
    matched_on: dict[str, Any],
) -> tuple[Match, str]:
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
            active=True,
        )
        session.add(row)
        session.flush()
        return row, "inserted"

    if not existing.active:
        # Matching again: keep status and alert history, refresh the evidence.
        existing.active = True
        existing.deactivated_at = None
        existing.inactive_reason = None
        existing.score = score
        existing.matched_on = matched_on
        session.flush()
        return existing, "reactivated"

    changed = existing.score != score or _stable(existing.matched_on) != _stable(matched_on)
    if changed:
        existing.score = score
        existing.matched_on = matched_on
        session.flush()
        return existing, "updated"
    return existing, "unchanged"


def _deactivate(session: Session, watchlist: Watchlist, keep: set[int], now: datetime, *, reason: str | None) -> int:
    """Mark this watchlist's active matches outside ``keep`` inactive. Returns the count.

    ``reason`` None means: closed opportunity -> opportunity_closed, otherwise
    no_longer_matches.
    """
    rows = session.execute(
        select(Match, Opportunity)
        .join(Opportunity, Match.opportunity_id == Opportunity.id)
        .where(Match.watchlist_id == watchlist.id, Match.active.is_(True))
    ).all()
    count = 0
    for match, opportunity in rows:
        if match.opportunity_id in keep:
            continue
        match.active = False
        match.deactivated_at = now
        if reason is not None:
            match.inactive_reason = reason
        elif not _is_open(opportunity, now):
            match.inactive_reason = INACTIVE_OPPORTUNITY_CLOSED
        else:
            match.inactive_reason = INACTIVE_NO_LONGER_MATCHES
        count += 1
    if count:
        session.flush()
    return count


def run_matching(
    session: Session,
    *,
    watchlist_id: int | None = None,
    now: datetime | None = None,
    rebuild: bool = False,
) -> MatchStats:
    """Evaluate watchlists, upsert their matches, and reconcile stale ones.

    Every run (scheduled, CLI, or UI) reconciles: matches a watchlist no longer
    produces are marked inactive, and matches of a disabled watchlist are
    marked inactive with reason ``watchlist_disabled``. ``rebuild`` is kept for
    callers that rebuild one watchlist explicitly; reconciliation is the same.
    """
    del rebuild  # reconciliation always runs; see the docstring
    stats = MatchStats()
    now = now or datetime.now(UTC)
    if watchlist_id is not None:
        watchlist = session.get(Watchlist, watchlist_id)
        if watchlist is None:
            raise ValueError(f"watchlist {watchlist_id} not found")
        watchlists = [watchlist]
    else:
        watchlists = list(session.scalars(select(Watchlist).order_by(Watchlist.id)))

    for watchlist in watchlists:
        if not watchlist.enabled:
            stats.removed += _deactivate(session, watchlist, set(), now, reason=INACTIVE_WATCHLIST_DISABLED)
            continue
        matched_opportunity_ids: set[int] = set()
        for opportunity in session.scalars(_opportunity_query(watchlist, now)):
            stats.evaluated += 1
            is_match, matched_on, score = evaluate_match(watchlist, opportunity, now=now)
            if not is_match:
                continue
            stats.matched += 1
            matched_opportunity_ids.add(opportunity.id)
            _, action = _upsert_match(
                session,
                opportunity_id=opportunity.id,
                watchlist_id=watchlist.id,
                score=score,
                matched_on=matched_on,
            )
            if action == "inserted":
                stats.inserted += 1
            elif action == "reactivated":
                stats.reactivated += 1
            elif action == "updated":
                stats.updated += 1
            else:
                stats.unchanged += 1

        watchlist.last_evaluated_at = now
        stats.removed += _deactivate(session, watchlist, matched_opportunity_ids, now, reason=None)

    return stats


