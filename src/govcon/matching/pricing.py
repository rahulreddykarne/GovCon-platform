"""Historical pricing from stored awards.

Unit price is returned only when the award row already has one. This module
does not divide an obligation by quantity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from govcon.models import Award

_NSN_GROUPS = re.compile(r"^(\d{4})-(\d{2})-(\d{3})-(\d{4})$")


@dataclass(frozen=True)
class PricePoint:
    award_id: str
    piid: str | None
    vendor_name: str | None
    vendor_uei: str | None
    action_date: date | None
    amount: Decimal | None
    unit_price: Decimal | None
    quantity: Decimal | None
    nsn: str | None
    psc_code: str | None
    naics_code: str | None
    awarding_agency: str | None
    description: str | None


def canonical_nsn(value: str | None) -> str | None:
    """Return a dashed 13-digit NSN, or None when the value is not one."""
    if value is None:
        return None
    compact = "".join(character for character in value if character.isdigit())
    if len(compact) != 13:
        return None
    dashed = f"{compact[:4]}-{compact[4:6]}-{compact[6:9]}-{compact[9:]}"
    if _NSN_GROUPS.fullmatch(dashed) is None:
        return None
    return dashed


def _point(row: Award) -> PricePoint:
    return PricePoint(
        award_id=row.award_id,
        piid=row.piid,
        vendor_name=row.recipient_name,
        vendor_uei=row.recipient_uei,
        action_date=row.action_date,
        amount=row.total_obligation,
        unit_price=row.unit_price,
        quantity=row.quantity,
        nsn=row.nsn,
        psc_code=row.psc_code,
        naics_code=row.naics_code,
        awarding_agency=row.awarding_agency,
        description=row.description,
    )


def _prefix(column, prefix: str):
    escaped = prefix.upper().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return func.upper(column).like(escaped + "%", escape="\\")


def _keyword_clause(keyword: str):
    escaped = re.escape(keyword.strip())
    return Award.description.op("~*")(rf"\y{escaped}\y")


def price_history(session: Session, nsn: str, *, limit: int = 50) -> list[PricePoint]:
    """Award history for one NSN, newest action date first."""
    normalized = canonical_nsn(nsn)
    if normalized is None or limit < 1:
        return []
    rows = session.scalars(
        select(Award)
        .where(Award.nsn == normalized)
        .order_by(Award.action_date.desc().nulls_last(), Award.id.desc())
        .limit(limit)
    ).all()
    return [_point(row) for row in rows]


def price_history_psc(
    session: Session,
    psc: str,
    keywords: list[str] | tuple[str, ...] | None = None,
    *,
    limit: int = 50,
) -> list[PricePoint]:
    """Awards whose PSC starts with ``psc``. Every keyword must match a whole word."""
    prefix = (psc or "").strip()
    if not prefix or limit < 1:
        return []
    statement = select(Award).where(_prefix(Award.psc_code, prefix))
    for keyword in keywords or []:
        cleaned = keyword.strip()
        if cleaned:
            statement = statement.where(_keyword_clause(cleaned))
    rows = session.scalars(
        statement.order_by(Award.action_date.desc().nulls_last(), Award.id.desc()).limit(limit)
    ).all()
    return [_point(row) for row in rows]


def _agency_tokens(value: str | None) -> set[str]:
    if not value:
        return set()
    stop = {"department", "of", "the", "and", "agency", "/", "-"}
    return {part for part in re.findall(r"[a-z0-9]+", value.lower()) if part not in stop and len(part) > 1}


def agencies_match(award_agency: str | None, opportunity_agency: str | None) -> bool:
    """True when the award's office looks like the solicitation's agency path."""
    left = _agency_tokens(award_agency)
    right = _agency_tokens(opportunity_agency)
    if not left or not right:
        return False
    if left & right:
        return True
    aliases = (
        ({"dla", "defense", "logistics"}, {"dla", "defense", "logistics", "spe7", "spe"}),
        ({"usda", "forest", "12c2"}, {"usda", "forest", "agriculture"}),
    )
    for group in aliases:
        if (left & group[0] or left & group[1]) and (right & group[0] or right & group[1]):
            return True
    return False


def comparable_relevance(
    point: PricePoint,
    *,
    nsn: str | None,
    psc_code: str | None,
    naics_code: str | None,
    awarding_agency: str | None,
) -> str:
    """``high`` when NSN or same agency plus PSC/NAICS match; ``low`` otherwise."""
    if nsn and point.nsn and canonical_nsn(point.nsn) == canonical_nsn(nsn):
        return "high"
    agency_ok = agencies_match(point.awarding_agency, awarding_agency)
    psc_ok = bool(psc_code and point.psc_code and point.psc_code.upper().startswith(psc_code[:2].upper()))
    naics_ok = bool(naics_code and point.naics_code and point.naics_code[:2] == naics_code[:2])
    if agency_ok and (psc_ok or naics_ok or not (psc_code or naics_code)):
        return "high"
    if (psc_ok or naics_ok) and not awarding_agency:
        return "high"
    if psc_ok or naics_ok:
        return "low"
    return "none"


def recent_award_comps(
    session: Session,
    *,
    nsn: str | None,
    psc_code: str | None,
    limit: int = 3,
    naics_code: str | None = None,
    awarding_agency: str | None = None,
    include_low_relevance: bool = False,
) -> list[PricePoint]:
    """Prefer an exact NSN history. Fall back to same-agency PSC/NAICS awards.

    Low-relevance hits (same PSC, different agency) are excluded from the
    score unless ``include_low_relevance`` is set.
    """
    found: list[PricePoint] = []
    if nsn:
        found.extend(price_history(session, nsn, limit=max(limit * 3, 10)))
    if psc_code and len(found) < limit * 3:
        found.extend(price_history_psc(session, psc_code, limit=max(limit * 3, 10)))
    seen: set[str] = set()
    ranked: list[tuple[str, PricePoint]] = []
    for point in found:
        if point.award_id in seen:
            continue
        seen.add(point.award_id)
        relevance = comparable_relevance(
            point, nsn=nsn, psc_code=psc_code, naics_code=naics_code, awarding_agency=awarding_agency,
        )
        if relevance == "none":
            continue
        if relevance == "low" and not include_low_relevance:
            continue
        ranked.append((relevance, point))
    ranked.sort(key=lambda item: (0 if item[0] == "high" else 1))
    return [point for _relevance, point in ranked[:limit]]
