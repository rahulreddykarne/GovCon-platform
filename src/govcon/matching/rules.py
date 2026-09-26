"""Deterministic watchlist rule evaluation.

Every non-empty rule group must pass. Values inside a group are OR'd.
Unknown opportunity values are not fabricated; filters record ``unknown``
in evidence instead of silently rejecting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from govcon.models import Opportunity, Watchlist


@dataclass(frozen=True)
class RuleResult:
    group: str
    configured: Any
    result: str
    detail: dict[str, Any]


@dataclass(frozen=True)
class EvaluationResult:
    matches: bool
    score: Decimal
    matched_on: dict[str, Any]
    vetoed: bool = False


def _normalize_list(values: list[str] | None) -> list[str]:
    if not values:
        return []
    return [value.strip() for value in values if value and value.strip()]


def _search_text(opportunity: Opportunity) -> str:
    parts = [opportunity.title or "", opportunity.description or ""]
    return " ".join(parts).lower()


def _prefix_match(field: str | None, prefixes: list[str]) -> tuple[bool, str | None]:
    if field is None:
        return False, None
    normalized = field.strip()
    for prefix in prefixes:
        if normalized.startswith(prefix):
            return True, prefix
    return False, None


def _keyword_hit(text: str, keywords: list[str]) -> list[str]:
    hits = []
    for keyword in keywords:
        if keyword.lower() in text:
            hits.append(keyword)
    return hits


def _opportunity_value_bounds(opportunity: Opportunity) -> tuple[Decimal | None, Decimal | None]:
    return opportunity.estimated_value_min, opportunity.estimated_value_max


def _days_until_deadline(opportunity: Opportunity, now: datetime) -> int | None:
    deadline = opportunity.response_deadline
    if deadline is None:
        return None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    delta = deadline - now.astimezone(UTC)
    return int(delta.total_seconds() // 86400)


def evaluate_watchlist(
    opportunity: Opportunity,
    watchlist: Watchlist,
    *,
    now: datetime | None = None,
) -> EvaluationResult:
    """Evaluate one opportunity against one watchlist."""
    now = now or datetime.now(UTC)
    evidence: dict[str, Any] = {}
    passed_groups = 0
    evaluated_groups = 0
    vetoed = False

    psc_codes = _normalize_list(watchlist.psc_codes)
    if psc_codes:
        evaluated_groups += 1
        matched, prefix = _prefix_match(opportunity.psc_code, psc_codes)
        evidence["psc"] = {
            "configured": psc_codes,
            "opportunity": opportunity.psc_code,
            "matched_prefix": prefix,
            "result": "pass" if matched else "fail",
        }
        if matched:
            passed_groups += 1
        else:
            return EvaluationResult(False, Decimal("0"), evidence)

    naics_codes = _normalize_list(watchlist.naics_codes)
    if naics_codes:
        evaluated_groups += 1
        matched, prefix = _prefix_match(opportunity.naics_code, naics_codes)
        evidence["naics"] = {
            "configured": naics_codes,
            "opportunity": opportunity.naics_code,
            "matched_prefix": prefix,
            "result": "pass" if matched else "fail",
        }
        if matched:
            passed_groups += 1
        else:
            return EvaluationResult(False, Decimal("0"), evidence)

    keywords = _normalize_list(watchlist.keywords)
    if keywords:
        evaluated_groups += 1
        text = _search_text(opportunity)
        hits = _keyword_hit(text, keywords)
        matched = bool(hits)
        evidence["keywords"] = {
            "configured": keywords,
            "matched": hits,
            "result": "pass" if matched else "fail",
        }
        if matched:
            passed_groups += 1
        else:
            return EvaluationResult(False, Decimal("0"), evidence)

    exclude_keywords = _normalize_list(watchlist.exclude_keywords)
    if exclude_keywords:
        evaluated_groups += 1
        text = _search_text(opportunity)
        hits = _keyword_hit(text, exclude_keywords)
        matched = not hits
        evidence["exclude_keywords"] = {
            "configured": exclude_keywords,
            "matched": hits,
            "result": "pass" if matched else "veto",
        }
        if hits:
            vetoed = True
            return EvaluationResult(False, Decimal("0"), evidence, vetoed=True)
        passed_groups += 1

    nsn_list = _normalize_list(watchlist.nsn_list)
    if nsn_list:
        evaluated_groups += 1
        matched = opportunity.nsn is not None and opportunity.nsn in nsn_list
        evidence["nsn"] = {
            "configured": nsn_list,
            "opportunity": opportunity.nsn,
            "result": "pass" if matched else "fail",
        }
        if matched:
            passed_groups += 1
        else:
            return EvaluationResult(False, Decimal("0"), evidence)

    set_asides = _normalize_list(watchlist.set_asides)
    if set_asides:
        evaluated_groups += 1
        matched = opportunity.set_aside_code is not None and opportunity.set_aside_code in set_asides
        evidence["set_asides"] = {
            "configured": set_asides,
            "opportunity": opportunity.set_aside_code,
            "result": "pass" if matched else "fail",
        }
        if matched:
            passed_groups += 1
        else:
            return EvaluationResult(False, Decimal("0"), evidence)

    sources = _normalize_list(watchlist.sources)
    if sources:
        evaluated_groups += 1
        matched = opportunity.source in sources
        evidence["sources"] = {
            "configured": sources,
            "opportunity": opportunity.source,
            "result": "pass" if matched else "fail",
        }
        if matched:
            passed_groups += 1
        else:
            return EvaluationResult(False, Decimal("0"), evidence)

    value_min, value_max = _opportunity_value_bounds(opportunity)
    value_known = value_min is not None or value_max is not None
    value_evidence: dict[str, Any] = {
        "opportunity_value_min": str(value_min) if value_min is not None else None,
        "opportunity_value_max": str(value_max) if value_max is not None else None,
    }

    if watchlist.min_value is not None:
        evaluated_groups += 1
        if not value_known:
            value_evidence["min_value"] = {
                "configured": str(watchlist.min_value),
                "result": "unknown",
            }
            passed_groups += 1
        else:
            compare_value = value_max if value_max is not None else value_min
            matched = compare_value is not None and compare_value >= watchlist.min_value
            value_evidence["min_value"] = {
                "configured": str(watchlist.min_value),
                "compared_value": str(compare_value),
                "result": "pass" if matched else "fail",
            }
            if not matched:
                evidence["value"] = value_evidence
                return EvaluationResult(False, Decimal("0"), evidence)
            passed_groups += 1

    if watchlist.max_value is not None:
        evaluated_groups += 1
        if not value_known:
            value_evidence["max_value"] = {
                "configured": str(watchlist.max_value),
                "result": "unknown",
            }
            passed_groups += 1
        else:
            compare_value = value_min if value_min is not None else value_max
            matched = compare_value is not None and compare_value <= watchlist.max_value
            value_evidence["max_value"] = {
                "configured": str(watchlist.max_value),
                "compared_value": str(compare_value),
                "result": "pass" if matched else "fail",
            }
            if not matched:
                evidence["value"] = value_evidence
                return EvaluationResult(False, Decimal("0"), evidence)
            passed_groups += 1

    if value_evidence.keys() - {"opportunity_value_min", "opportunity_value_max"}:
        evidence["value"] = value_evidence

    if watchlist.min_deadline_days is not None:
        evaluated_groups += 1
        days_remaining = _days_until_deadline(opportunity, now)
        if days_remaining is None:
            evidence["deadline"] = {
                "configured_days": watchlist.min_deadline_days,
                "days_remaining": None,
                "result": "unknown",
            }
            passed_groups += 1
        else:
            matched = days_remaining >= watchlist.min_deadline_days
            evidence["deadline"] = {
                "configured_days": watchlist.min_deadline_days,
                "days_remaining": days_remaining,
                "result": "pass" if matched else "fail",
            }
            if matched:
                passed_groups += 1
            else:
                return EvaluationResult(False, Decimal("0"), evidence)

    score = Decimal(str(passed_groups)) if evaluated_groups else Decimal("0")
    return EvaluationResult(True, score, evidence)
