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


def recent_award_comps(
    session: Session,
    *,
    nsn: str | None,
    psc_code: str | None,
    limit: int = 3,
) -> list[PricePoint]:
    """Prefer an exact NSN history. Fall back to the PSC prefix when that is empty."""
    if nsn:
        exact = price_history(session, nsn, limit=limit)
        if exact:
            return exact
    if psc_code:
        return price_history_psc(session, psc_code, limit=limit)
    return []
