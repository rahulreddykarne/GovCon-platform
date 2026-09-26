"""Award history, awardee totals, and the recompete view.

Totals sum stored obligations. They do not invent a unit price.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from govcon.matching.pricing import PricePoint, _point, _prefix, canonical_nsn
from govcon.models import Award


@dataclass(frozen=True)
class AwardeeTotal:
    recipient_name: str | None
    recipient_uei: str | None
    award_count: int
    total_obligation: Decimal | None


@dataclass(frozen=True)
class RecompeteCandidate:
    award_id: str
    piid: str | None
    recipient_name: str | None
    awarding_agency: str | None
    psc_code: str | None
    naics_code: str | None
    nsn: str | None
    action_date: date | None
    period_end: date | None
    total_obligation: Decimal | None
    heuristic: str


def _contains(column, value: str):
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return column.ilike(f"%{escaped}%", escape="\\")


def award_history_for_agency(
    session: Session,
    agency: str,
    *,
    psc: str | None = None,
    naics: str | None = None,
    nsn: str | None = None,
    limit: int = 50,
) -> list[PricePoint]:
    """Awards whose awarding agency path contains ``agency``."""
    needle = (agency or "").strip()
    if not needle or limit < 1:
        return []
    statement = select(Award).where(_contains(Award.awarding_agency, needle))
    if psc and psc.strip():
        statement = statement.where(_prefix(Award.psc_code, psc.strip()))
    if naics and naics.strip():
        statement = statement.where(_prefix(Award.naics_code, naics.strip()))
    normalized = canonical_nsn(nsn) if nsn else None
    if nsn and normalized is None:
        return []
    if normalized is not None:
        statement = statement.where(Award.nsn == normalized)
    rows = session.scalars(
        statement.order_by(Award.action_date.desc().nulls_last(), Award.id.desc()).limit(limit)
    ).all()
    return [_point(row) for row in rows]


def top_awardees(
    session: Session,
    *,
    psc: str | None = None,
    naics: str | None = None,
    nsn: str | None = None,
    limit: int = 10,
) -> list[AwardeeTotal]:
    """Recipients ordered by award count, then total obligation."""
    if limit < 1:
        return []
    normalized = canonical_nsn(nsn) if nsn else None
    if nsn and normalized is None:
        return []
    group_key = func.coalesce(Award.recipient_uei, Award.recipient_name)
    statement = select(
        func.max(Award.recipient_uei),
        func.max(Award.recipient_name),
        func.count().label("award_count"),
        func.sum(Award.total_obligation).label("total_obligation"),
    )
    if psc and psc.strip():
        statement = statement.where(_prefix(Award.psc_code, psc.strip()))
    if naics and naics.strip():
        statement = statement.where(_prefix(Award.naics_code, naics.strip()))
    if normalized is not None:
        statement = statement.where(Award.nsn == normalized)
    statement = statement.group_by(group_key).order_by(
        func.count().desc(),
        func.sum(Award.total_obligation).desc().nulls_last(),
    ).limit(limit)
    totals: list[AwardeeTotal] = []
    for recipient_uei, recipient_name, award_count, total_obligation in session.execute(statement):
        totals.append(
            AwardeeTotal(
                recipient_name=recipient_name,
                recipient_uei=recipient_uei,
                award_count=int(award_count),
                total_obligation=total_obligation,
            )
        )
    return totals


def _safe_prefix(value: str) -> str:
    return value.upper().replace("\\", "").replace("%", "").replace("_", "")


def recompete_candidates(
    session: Session,
    *,
    psc: str | None = None,
    naics: str | None = None,
    nsn: str | None = None,
    limit: int = 100,
) -> list[RecompeteCandidate]:
    """Older awards from ``award_recompete_candidates``.

    An award is included when its action date is at least 18 months ago and
    its period end is missing or within the next 18 months. A long remaining
    period keeps the award out of the view.
    """
    if limit < 1:
        return []
    clauses = ["1=1"]
    params: dict[str, object] = {"limit": limit}
    if psc and psc.strip():
        clauses.append("upper(psc_code) LIKE :psc")
        params["psc"] = _safe_prefix(psc.strip()) + "%"
    if naics and naics.strip():
        clauses.append("upper(naics_code) LIKE :naics")
        params["naics"] = _safe_prefix(naics.strip()) + "%"
    if nsn:
        normalized = canonical_nsn(nsn)
        if normalized is None:
            return []
        clauses.append("nsn = :nsn")
        params["nsn"] = normalized
    statement = text(
        f"""
        SELECT award_id, piid, recipient_name, awarding_agency, psc_code, naics_code, nsn,
               action_date, period_end, total_obligation, heuristic
        FROM award_recompete_candidates
        WHERE {" AND ".join(clauses)}
        ORDER BY action_date ASC, award_id ASC
        LIMIT :limit
        """
    )
    rows = session.execute(statement, params)
    return [
        RecompeteCandidate(
            award_id=row.award_id,
            piid=row.piid,
            recipient_name=row.recipient_name,
            awarding_agency=row.awarding_agency,
            psc_code=row.psc_code,
            naics_code=row.naics_code,
            nsn=row.nsn,
            action_date=row.action_date,
            period_end=row.period_end,
            total_obligation=row.total_obligation,
            heuristic=row.heuristic,
        )
        for row in rows
    ]
