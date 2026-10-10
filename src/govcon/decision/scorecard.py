"""Bid/no-bid scorecard: one row per factor with score, weight, evidence and gaps.

The weighted score is only computed over factors that have an input. Missing
inputs stay missing; they lower confidence instead of being scored as a fail.
Estimated market prices are labeled as estimates. Solicitation dollar amounts
are never treated as revenue.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

FACTORS: tuple[tuple[str, str, float], ...] = (
    ("eligibility", "Eligibility / set-aside", 0.18),
    ("past_performance", "Past performance", 0.12),
    ("capability", "Capability fit", 0.15),
    ("price_margin", "Price / margin", 0.15),
    ("competition", "Competition / award history", 0.10),
    ("compliance", "Compliance blockers", 0.15),
    ("schedule", "Schedule", 0.08),
    ("risk", "Risk", 0.07),
)
RECOMMENDATION_LABELS = {
    "bid": "BID",
    "no_bid": "NO-BID",
    "insufficient_information": "NEEDS-INFO",
    "review": "REVIEW",
}
COST_BASIS_LABELS = {
    "pursuit": "Recorded supplier cost",
    "supplier_quote": "Supplier quote",
    "web_estimate": "Market-price median (estimate)",
}


@dataclass
class Factor:
    key: str
    label: str
    weight: float
    score: float | None
    evidence: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def known(self) -> bool:
        return self.score is not None and not self.missing


def _clamp(value: float) -> float:
    return round(min(max(value, 0.0), 1.0), 4)


def _margin_score(margin_pct: float | None) -> float | None:
    if margin_pct is None:
        return None
    if margin_pct < 0:
        return 0.0
    if margin_pct < 5:
        return 0.15
    if margin_pct < 12:
        return 0.4
    if margin_pct < 20:
        return 0.7
    return 0.9


def _eligibility(state: dict[str, Any]) -> Factor:
    elig = state.get("eligibility") or {}
    opp = state.get("opportunity") or {}
    provenance = state.get("provenance") or {}
    missing: list[str] = []
    evidence: list[str] = []
    parts: list[float] = []
    for name, label in (("set_aside_match", "set-aside eligibility"), ("sam_active", "SAM registration")):
        value = elig.get(name)
        detail = (provenance.get(name) or {}).get("detail")
        if value is True:
            parts.append(1.0)
            evidence.append(detail or f"{label} is met")
        elif value is False:
            parts.append(0.0)
            evidence.append(detail or f"{label} is not met")
        else:
            missing.append(detail or f"{label} is not in company facts")
    if opp.get("set_aside"):
        evidence.append(f"Notice set-aside: {opp.get('set_aside')}")
    else:
        evidence.append("No set-aside on this notice")
    certs = elig.get("mandatory_certifications_met")
    if certs is False:
        parts.append(0.0)
        evidence.append((provenance.get("mandatory_certifications_met") or {}).get("detail") or "A required certification is not held")
    elif certs is None:
        missing.append((provenance.get("mandatory_certifications_met") or {}).get("detail") or "required certifications are not established")
    elif certs is True:
        parts.append(1.0)
        evidence.append((provenance.get("mandatory_certifications_met") or {}).get("detail") or "Listed certifications are held")
    if missing and not any(p == 0.0 for p in parts):
        score = None
    elif parts:
        score = _clamp(sum(parts) / len(parts))
    else:
        score = None
    return Factor("eligibility", "Eligibility / set-aside", 0.18, score, evidence, missing)


def _past_performance(state: dict[str, Any]) -> Factor:
    company = state.get("company_inputs") or {}
    records = [str(r) for r in company.get("past_performance") or [] if str(r).strip()]
    capability = (state.get("provenance") or {}).get("capability_fit_score") or {}
    detail = capability.get("detail") or ""
    wins = 0
    if "past win" in detail:
        evidence = [detail]
        for token in detail.split():
            if token.isdigit():
                wins += int(token)
                break
    else:
        evidence = []
    if records:
        evidence.append(f"{len(records)} past performance record(s) on file: " + "; ".join(records[:3]) + ("…" if len(records) > 3 else ""))
    if not records and not wins:
        return Factor("past_performance", "Past performance", 0.12, None, evidence,
                      ["missing company fact: past performance"])
    score = _clamp(0.35 + 0.15 * min(len(records), 3) + 0.15 * min(wins, 2))
    return Factor("past_performance", "Past performance", 0.12, score, evidence, [])


def _capability(state: dict[str, Any]) -> Factor:
    score = (state.get("scores") or {}).get("capability_fit_score")
    provenance = (state.get("provenance") or {}).get("capability_fit_score") or {}
    if score is None:
        return Factor("capability", "Capability fit", 0.15, None,
                      [], [provenance.get("detail") or "no watchlist match, past win, or profile NAICS"])
    return Factor("capability", "Capability fit", 0.15, _clamp(float(score)),
                  [provenance.get("detail") or "capability score from watchlists and history"], [])


def _price_margin(state: dict[str, Any]) -> Factor:
    pricing = state.get("pricing") or {}
    margin = pricing.get("margin_pct")
    basis = pricing.get("cost_basis")
    basis_label = COST_BASIS_LABELS.get(basis or "", pricing.get("cost_basis_detail") or "cost basis not recorded")
    evidence: list[str] = []
    missing: list[str] = []
    if margin is None:
        missing.append("price or cost is not recorded, so the margin is unknown")
    else:
        evidence.append(f"Margin {margin}% using {basis_label}")
        if pricing.get("margin_basis"):
            evidence.append(pricing["margin_basis"])
    if basis == "web_estimate":
        evidence.append("Cost is a market-price median estimate, not a supplier quote")
    elif basis == "supplier_quote":
        evidence.append(pricing.get("cost_basis_detail") or "Lowest current supplier quote")
    elif basis == "pursuit":
        evidence.append(pricing.get("cost_basis_detail") or "Cost entered on the pursuit")
    if pricing.get("historical_median_unit_price") is not None:
        evidence.append(
            f"Comparable award median unit price ${pricing['historical_median_unit_price']:,.2f} "
            f"({int(pricing.get('historical_comparable_count') or 0)} award(s); estimate, not a quote)"
        )
    return Factor("price_margin", "Price / margin", 0.15, _margin_score(margin if isinstance(margin, (int, float)) else None),
                  evidence, missing)


def _competition(state: dict[str, Any]) -> Factor:
    signals = state.get("signals") or {}
    if "distinct_awardee_count" not in signals and "competitor_bucket_count" not in signals:
        return Factor("competition", "Competition / award history", 0.10, None, [],
                      ["award history for this NSN/PSC has not been pulled"])
    awardees = int(signals.get("distinct_awardee_count") or 0)
    evidence = [f"{awardees} distinct awardee(s) on comparable awards"]
    if signals.get("repeat_awardee_signal"):
        evidence.append((state.get("provenance") or {}).get("repeat_awardee_signal", {}).get("detail") or "A repeat awardee appears in comparable awards")
        score = 0.25 if awardees >= 5 else 0.4
    elif awardees >= 5:
        score = 0.35
    elif awardees == 0:
        score = 0.7
        evidence.append("No comparable awards on file; competition is unknown rather than low")
    else:
        score = 0.55
    return Factor("competition", "Competition / award history", 0.10, score, evidence, [])


def _compliance(state: dict[str, Any]) -> Factor:
    compliance = state.get("compliance") or {}
    total = int(compliance.get("mandatory_total") or 0)
    unmet = int(compliance.get("mandatory_unmet") or 0)
    unresolved = int(compliance.get("mandatory_missing") or 0)
    critical = int(compliance.get("critical_unresolved") or 0)
    if total == 0:
        return Factor("compliance", "Compliance blockers", 0.15, None, [],
                      ["compliance matrix has not extracted mandatory requirements"])
    evidence = [f"{unmet} unmet, {unresolved} unresolved of {total} mandatory; {critical} critical unresolved"]
    if unmet or critical:
        return Factor("compliance", "Compliance blockers", 0.15, 0.0, evidence, [])
    if unresolved:
        return Factor("compliance", "Compliance blockers", 0.15, 0.25, evidence,
                      [f"{unresolved} mandatory item(s) still unknown or awaiting review"])
    return Factor("compliance", "Compliance blockers", 0.15, 0.9, evidence, [])


def _schedule(state: dict[str, Any]) -> Factor:
    days = (state.get("opportunity") or {}).get("days_remaining")
    lead = (state.get("sourcing") or {}).get("lead_time_days")
    required = (state.get("opportunity") or {}).get("required_delivery_days")
    evidence: list[str] = []
    missing: list[str] = []
    if days is None:
        missing.append("response deadline is not known")
    elif isinstance(days, (int, float)):
        evidence.append(f"{int(days)} day(s) remaining")
    if isinstance(lead, (int, float)) and isinstance(required, (int, float)):
        evidence.append(f"Supplier lead time {lead} days vs required {required} days")
        if lead > required:
            return Factor("schedule", "Schedule", 0.08, 0.1, evidence, missing)
    elif required is not None and lead is None:
        missing.append("supplier lead time is not documented")
    if days is None:
        return Factor("schedule", "Schedule", 0.08, None, evidence, missing)
    if days < 0:
        score = 0.0
    elif days <= 2:
        score = 0.15
    elif days <= 5:
        score = 0.35
    elif days <= 10:
        score = 0.6
    else:
        score = 0.85
    return Factor("schedule", "Schedule", 0.08, score, evidence, missing)


def _risk(state: dict[str, Any]) -> Factor:
    compliance = state.get("compliance") or {}
    amendment = state.get("amendment") or {}
    analysis = state.get("analysis") or {}
    evidence: list[str] = []
    deductions = 0.0
    if compliance.get("country_of_origin_conflict"):
        deductions += 0.5
        evidence.append("Prohibited country-of-origin conflict")
    if amendment.get("material") is True:
        deductions += 0.2
        evidence.append("A material amendment is on file")
    flags = analysis.get("risk_flags") or []
    if flags:
        deductions += min(0.3, 0.1 * len(flags))
        evidence.append(f"{len(flags)} risk flag(s) on the solicitation analysis")
    if not evidence:
        evidence.append("No named risk flags on the current analysis")
    return Factor("risk", "Risk", 0.07, _clamp(0.8 - deductions), evidence, [])


BUILDERS = {
    "eligibility": _eligibility,
    "past_performance": _past_performance,
    "capability": _capability,
    "price_margin": _price_margin,
    "competition": _competition,
    "compliance": _compliance,
    "schedule": _schedule,
    "risk": _risk,
}


@dataclass
class Scorecard:
    factors: list[Factor]
    weighted_score: float | None
    coverage: float
    missing_inputs: list[str]
    estimated_value_note: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "factors": [
                {"key": f.key, "label": f.label, "weight": f.weight, "score": f.score,
                 "evidence": f.evidence, "missing": f.missing, "known": f.known}
                for f in self.factors
            ],
            "weighted_score": self.weighted_score,
            "coverage": round(self.coverage, 4),
            "missing_inputs": self.missing_inputs,
            "estimated_value_note": self.estimated_value_note,
        }


def estimated_value_note(opportunity: dict[str, Any]) -> str | None:
    """Solicitation dollar range, labeled so it cannot be read as revenue."""
    low, high = opportunity.get("estimated_value_min"), opportunity.get("estimated_value_max")
    if low is None and high is None:
        return None
    if low is not None and high is not None and low != high:
        amount = f"${low:,.0f}–${high:,.0f}"
    else:
        amount = f"${(high if high is not None else low):,.0f}"
    return f"Solicitation estimated value {amount} (not revenue; not a bid amount)"


def build_scorecard(state: dict[str, Any]) -> Scorecard:
    factors = [BUILDERS[key](state) for key, _label, _weight in FACTORS]
    known_weight = sum(f.weight for f in factors if f.known)
    total_weight = sum(f.weight for f in factors)
    coverage = known_weight / total_weight if total_weight else 0.0
    if known_weight:
        weighted = sum((f.score or 0.0) * f.weight for f in factors if f.known) / known_weight
    else:
        weighted = None
    missing = [item for f in factors for item in f.missing]
    return Scorecard(
        factors=factors,
        weighted_score=round(weighted, 4) if weighted is not None else None,
        coverage=coverage,
        missing_inputs=missing,
        estimated_value_note=estimated_value_note(state.get("opportunity") or {}),
    )
