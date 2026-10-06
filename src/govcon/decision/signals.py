"""Evidence-backed decision signals.

Every factor is three-state (``True`` / ``False`` / ``None`` = unknown) and
carries provenance: where it came from and how much to trust it. Unknown is
never treated as a failure; only real evidence produces ``False``.

The company eligibility profile is the approved company-facts file
(``COMPANY_FACTS_PATH``), the same data the compliance validators use:

    {
      "uei": "ABC123DEF456",
      "sam_registration_status": "Active",
      "sam_expiration_date": "2027-06-30",
      "socioeconomic": {"small_business": true, "8a": false, "hubzone": false, ...},
      "certifications": ["ISO 9001", "AS9100"],
      "naics_codes": ["339112", "423450"]
    }
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from govcon.compliance import deterministic as det
from govcon.config import Settings, get_settings
from govcon.intelligence.competitors import CompetitorSummary
from govcon.matching.pricing import PricePoint
from govcon.models import (
    ComplianceFinding,
    ComplianceRun,
    Match,
    Opportunity,
    OutcomeFeedback,
    Pursuit,
    Requirement,
    RequirementEvidence,
    Vendor,
    Watchlist,
)

logger = logging.getLogger("govcon.decision.signals")

_TRISTATE = {"pass": True, "fail": False, "unknown": None}


@dataclass
class Signals:
    values: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, dict[str, Any]] = field(default_factory=dict)

    def set(self, name: str, value: Any, *, source: str, confidence: str, detail: str | None = None) -> None:
        self.values[name] = value
        entry: dict[str, Any] = {"source": source, "confidence": confidence}
        if detail:
            entry["detail"] = detail
        self.provenance[name] = entry


def load_company_profile(settings: Settings | None = None, session: Any = None) -> dict[str, Any]:
    from govcon.compliance.pipeline import load_company_facts

    try:
        return load_company_facts(settings or get_settings(), session)
    except (OSError, ValueError) as exc:
        logger.warning("company facts could not be read: %s", exc)
        return {}


def eligibility_signals(
    session: Session,
    opportunity: Opportunity,
    profile: dict[str, Any],
    summary_json: dict[str, Any],
    *,
    has_summary: bool,
    settings: Settings | None = None,
    signals: Signals,
) -> None:
    # Set-aside: compared against the company's declared socioeconomic status.
    set_aside = det.set_aside_matches(opportunity.set_aside_code, profile)
    signals.set(
        "set_aside_match",
        _TRISTATE[set_aside.status],
        source="company_facts.socioeconomic" if opportunity.set_aside_code else "opportunity.set_aside_code",
        confidence="high" if set_aside.status != "unknown" else "unknown",
        detail=set_aside.reason,
    )

    # SAM registration: company facts first, then our own cached SAM entity.
    # The vendor cache is used only when it is itself fresh or a known expiry.
    # A stale, failed, or older registration must not become a positive signal.
    sam = det.sam_registration_known(profile, opportunity.response_deadline)
    sam_value = _TRISTATE[sam.status]
    sam_source = "company_facts.sam_registration_status"
    sam_detail = sam.reason
    # A company-registration overlay already decided, including "unknown".
    # Do not replace that with a vendor-cache hit.
    if sam_value is None and profile.get("uei") and not (profile.get("_provenance") or {}).get("sam_registration"):
        vendor = _own_vendor(session, str(profile["uei"]), settings)
        settings = settings or get_settings()
        now = datetime.now(UTC)
        from govcon.ingest.freshness import evidence_status

        status = evidence_status(
            vendor, now=now, max_age=timedelta(days=settings.company_facts_max_age_days),
        ) if vendor is not None else "unknown"
        sam_source = "sam_entity_api (own UEI)"
        if status == "expired":
            sam_value = False
            sam_detail = "SAM registration is expired"
        elif status == "fresh" and vendor is not None and vendor.registration_status:
            registration = (vendor.raw or {}).get("entityRegistration") if isinstance(vendor.raw, dict) else None
            expiration = registration.get("registrationExpirationDate") if isinstance(registration, dict) else None
            if expiration is None and vendor.expires_at is not None:
                expiration = vendor.expires_at.isoformat()
            try:
                checked = det.sam_registration_known(
                    {"sam_registration_status": vendor.registration_status, "sam_expiration_date": expiration},
                    opportunity.response_deadline or now,
                )
                sam_value = _TRISTATE[checked.status]
                sam_detail = checked.reason
            except (ValueError, TypeError):
                sam_value = None
                sam_detail = f"vendor cache {status}; registration could not be read"
        else:
            sam_value = None
            sam_detail = f"SAM entity cache is {status}; not used as current registration evidence"
    signals.set(
        "sam_active",
        sam_value,
        source=sam_source,
        confidence="high" if sam_value is not None else "unknown",
        detail=sam_detail,
    )

    # Certifications: listed by the solicitation analysis ≠ held by the company.
    required = [str(c).strip() for c in summary_json.get("certifications") or [] if str(c).strip()]
    held = profile.get("certifications")
    if not has_summary:
        value, detail = None, "solicitation analysis has not run"
    elif not required:
        value, detail = True, "the solicitation analysis lists no required certifications"
    elif not isinstance(held, list):
        value, detail = None, "company facts do not list held certifications"
    else:
        held_l = [str(h).strip().lower() for h in held if str(h).strip()]
        missing = [r for r in required if not any(r.lower() in h or h in r.lower() for h in held_l)]
        value = not missing
        detail = f"not held: {', '.join(missing)}" if missing else "all listed certifications are held"
    signals.set(
        "mandatory_certifications_met",
        value,
        source="solicitation_analysis.certifications vs company_facts.certifications",
        confidence="medium" if value is not None else "unknown",
        detail=detail,
    )


def _own_vendor(session: Session, uei: str, settings: Settings | None) -> Vendor | None:
    from govcon.ingest.freshness import evidence_status
    from govcon.ingest.sam_entities import SamEntityError, ensure_vendor

    normalized = uei.strip().upper()
    vendor = session.get(Vendor, normalized)
    if vendor is not None:
        session.refresh(vendor)
    settings = settings or get_settings()
    now = datetime.now(UTC)
    max_age = timedelta(days=settings.company_facts_max_age_days)
    if vendor is not None and evidence_status(vendor, now=now, max_age=max_age) in {"fresh", "expired"}:
        return vendor
    if not settings.sam_api_key:
        return None
    try:
        vendor, _ = ensure_vendor(session, normalized, refresh=True, settings=settings, now=now)
    except SamEntityError:
        logger.warning("own SAM registration lookup failed")
        return None
    if vendor is not None and evidence_status(vendor, now=now, max_age=max_age) in {"fresh", "expired"}:
        return vendor
    return None


def sourcing_signals(pursuit: Pursuit | None, signals: Signals) -> None:
    """Only recorded supplier/product results count; AI line items do not."""
    if pursuit is not None and (pursuit.supplier or pursuit.sourcing_cost is not None):
        signals.set(
            "product_found",
            True,
            source="pursuit.supplier/sourcing_cost",
            confidence="medium",
            detail=f"supplier={pursuit.supplier!r}",
        )
    else:
        signals.set("product_found", None, source="none", confidence="unknown", detail="no supplier or sourcing cost recorded")


SUPPLIER_EVIDENCE_TYPES = ("supplier_quote", "supplier_spec_sheet")


def supplier_lead_time_signal(session: Session, opportunity_id: int, signals: Signals) -> None:
    """Supplier lead time only from verified supplier evidence; unknown otherwise.

    The buyer's required delivery period is a requirement, never evidence of
    what a supplier can do. Evidence is a verified ``supplier_quote`` or
    ``supplier_spec_sheet`` row whose ``evidence_value`` holds a
    non-negative ``lead_time_days`` (``govcon compliance evidence-add
    --value '{"lead_time_days": 21}'``); the latest one counts.
    """
    rows = session.execute(
        select(RequirementEvidence)
        .join(Requirement, RequirementEvidence.requirement_id == Requirement.id)
        .where(
            Requirement.opportunity_id == opportunity_id,
            RequirementEvidence.evidence_type.in_(SUPPLIER_EVIDENCE_TYPES),
            RequirementEvidence.verification_status == "verified",
        )
        .order_by(RequirementEvidence.created_at.desc(), RequirementEvidence.id.desc())
    ).scalars().all()
    for row in rows:
        value = (row.evidence_value or {}).get("lead_time_days") if isinstance(row.evidence_value, dict) else None
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value < 0
            or (isinstance(value, float) and math.isnan(value))
        ):
            continue
        signals.set(
            "supplier_lead_time_days",
            value,
            source=f"requirement_evidence:{row.id}",
            confidence="medium",
            detail=f"{row.evidence_type} recorded {row.created_at.isoformat() if row.created_at else 'unknown date'}"
            + (f": {row.description}" if row.description else ""),
        )
        return
    signals.set(
        "supplier_lead_time_days", None, source="none", confidence="unknown",
        detail="no verified supplier quote or spec sheet states a lead time",
    )


def capability_signal(session: Session, opportunity: Opportunity, profile: dict[str, Any], signals: Signals) -> None:
    """Fit from the company's own targeting and record, not from field presence."""
    evidence: list[str] = []
    score = 0.4
    matched = session.scalar(
        select(func.count())
        .select_from(Match)
        .join(Watchlist, Match.watchlist_id == Watchlist.id)
        .where(
            Match.opportunity_id == opportunity.id,
            Watchlist.enabled.is_(True),
            Match.active.is_(True),
            Match.status != "dismissed",
        )
    )
    if matched:
        score += 0.25
        evidence.append(f"matches {matched} enabled watchlist(s)")
    if opportunity.psc_code:
        psc_wins = session.scalar(
            select(func.count())
            .select_from(OutcomeFeedback)
            .where(OutcomeFeedback.outcome == "won", OutcomeFeedback.denorm_psc == opportunity.psc_code)
        )
        if psc_wins:
            score += 0.2
            evidence.append(f"{psc_wins} past win(s) in PSC {opportunity.psc_code}")
    if opportunity.naics_code:
        naics_wins = session.scalar(
            select(func.count())
            .select_from(OutcomeFeedback)
            .where(OutcomeFeedback.outcome == "won", OutcomeFeedback.denorm_naics == opportunity.naics_code)
        )
        if naics_wins:
            score += 0.1
            evidence.append(f"{naics_wins} past win(s) in NAICS {opportunity.naics_code}")
        naics_profile = [str(n) for n in profile.get("naics_codes") or []]
        if any(opportunity.naics_code.startswith(n) for n in naics_profile if n):
            score += 0.15
            evidence.append("NAICS is in the company profile")
    if not evidence:
        signals.set("capability_fit_score", None, source="none", confidence="unknown", detail="no watchlist match, past win, or profile NAICS")
        return
    signals.set(
        "capability_fit_score",
        round(min(score, 0.95), 2),
        source="watchlists, outcome history, company_facts.naics_codes",
        confidence="medium" if len(evidence) > 1 else "low",
        detail="; ".join(evidence),
    )


def pricing_signals(
    opportunity: Opportunity, pursuit: Pursuit | None, comps: list[PricePoint], signals: Signals,
    *, product_quantity: Decimal | None = None,
) -> None:
    """Prices are compared per unit, and only with awards that state a unit price."""
    unit_prices = [float(p.unit_price) for p in comps if p.unit_price is not None]
    totals = [float(p.amount) for p in comps if p.amount is not None]
    signals.set(
        "historical_median_unit_price",
        float(median(unit_prices)) if unit_prices else None,
        source="awards.unit_price",
        confidence="medium" if len(unit_prices) >= 3 else ("low" if unit_prices else "unknown"),
        detail=f"{len(unit_prices)} comparable award(s) with a stated unit price",
    )
    signals.values["historical_unit_price_count"] = len(unit_prices)
    signals.values["historical_median_award_total"] = float(median(totals)) if totals else None
    resolved_quantity = product_quantity if product_quantity is not None else opportunity.quantity
    quantity = float(resolved_quantity) if resolved_quantity is not None and resolved_quantity > 0 else None
    price = float(pursuit.quote_price) if pursuit and pursuit.quote_price is not None else None
    cost = float(pursuit.sourcing_cost) if pursuit and pursuit.sourcing_cost is not None else None
    signals.values["proposed_unit_price"] = round(price / quantity, 6) if price is not None and quantity else None
    signals.values["unit_cost"] = round(cost / quantity, 6) if cost is not None and quantity else None


def competition_signals(summary: CompetitorSummary | None, signals: Signals) -> None:
    """Repeat awardees in the same NSN/PSC; a heuristic, not a verified incumbent."""
    awardees: dict[str, int] = {}
    repeat: list[str] = []
    for bucket in summary.buckets if summary else ():
        for winner in bucket.winners:
            key = winner.recipient_name or winner.recipient_uei or "?"
            awardees[key] = max(awardees.get(key, 0), winner.award_count)
            if bucket.dimension in {"nsn", "psc"} and winner.award_count >= 2 and key not in repeat:
                repeat.append(key)
    signals.values["distinct_awardee_count"] = len(awardees)
    signals.set(
        "repeat_awardee_signal",
        bool(repeat),
        source="awards (same NSN/PSC)",
        confidence="low",
        detail=f"repeat awardees: {', '.join(repeat)}" if repeat else "no awardee with 2+ comparable awards",
    )


def amendment_signal(session: Session, opportunity_id: int, amendment_count: int, signals: Signals) -> None:
    """Material only when amendment revalidation (or an open reopen finding) says so."""
    run = session.scalar(
        select(ComplianceRun)
        .where(ComplianceRun.opportunity_id == opportunity_id, ComplianceRun.run_type == "amendment_revalidation")
        .order_by(ComplianceRun.created_at.desc(), ComplianceRun.id.desc())
        .limit(1)
    )
    material: bool | None = None
    source = "none"
    if run is not None and isinstance(run.output_json, dict):
        material = bool((run.output_json.get("impact") or {}).get("material"))
        source = "compliance.amendment_revalidation"
    reopen = session.scalar(
        select(func.count())
        .select_from(ComplianceFinding)
        .where(
            ComplianceFinding.opportunity_id == opportunity_id,
            ComplianceFinding.finding_type == "review_reopen_required",
            ComplianceFinding.status == "open",
        )
    )
    if reopen:
        material, source = True, "open review_reopen_required finding"
    if material is None:
        material = False if amendment_count == 0 else None
    signals.set(
        "amendment_material",
        material,
        source=source,
        confidence="high" if source != "none" else "unknown",
        detail=f"{amendment_count} amendment(s) in the solicitation analysis",
    )
