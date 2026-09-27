"""Source-backed supplier leads from historical awards.

An award recipient is a research lead, not a verified supplier or a current
quote. Registration status is shown only when a SAM entity record was fetched.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from govcon.models import Award, Opportunity, Vendor


@dataclass(frozen=True)
class SupplierLead:
    name: str | None
    uei: str | None
    award_count: int
    last_award: date | None
    registration_status: str | None
    registration_checked_at: datetime | None
    match_basis: str


def supplier_leads(session: Session, opportunity: Opportunity, *, limit: int = 20) -> list[SupplierLead]:
    """Rank past awardees with the same NSN, falling back to the same PSC."""
    if limit < 1 or (not opportunity.nsn and not opportunity.psc_code):
        return []
    def find(match):
        return session.execute(
            select(
                Award.recipient_uei, Award.recipient_name,
                func.count(Award.id), func.max(Award.action_date),
            )
            .where(match, (Award.recipient_uei.is_not(None) | Award.recipient_name.is_not(None)))
            .group_by(Award.recipient_uei, Award.recipient_name)
            .order_by(func.count(Award.id).desc(), func.max(Award.action_date).desc().nullslast())
            .limit(limit)
        ).all()

    rows = find(Award.nsn == opportunity.nsn) if opportunity.nsn else []
    basis = "exact NSN"
    if not rows and opportunity.psc_code:
        rows = find(Award.psc_code == opportunity.psc_code)
        basis = "same PSC"
    leads = []
    for uei, name, count, last_award in rows:
        vendor = session.get(Vendor, uei) if uei else None
        leads.append(SupplierLead(
            name=name, uei=uei, award_count=int(count), last_award=last_award,
            registration_status=vendor.registration_status if vendor else None,
            registration_checked_at=vendor.fetched_at if vendor else None,
            match_basis=basis,
        ))
    return leads
