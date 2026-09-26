"""Buyer contact search (Phase 6)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from govcon.models import Contact


@dataclass(frozen=True)
class ContactRecord:
    id: int
    name: str | None
    email: str | None
    phone: str | None
    title: str | None
    agency_path: str | None
    contact_type: str | None
    first_seen_opportunity_id: int | None


def _contains(column, value: str):
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return column.ilike(f"%{escaped}%", escape="\\")


def search_contacts(
    session: Session,
    *,
    name: str | None = None,
    agency: str | None = None,
    email: str | None = None,
    limit: int = 50,
) -> list[ContactRecord]:
    """Search harvested buyer contacts by name, agency path, or email."""
    if limit < 1:
        return []
    name = (name or "").strip() or None
    agency = (agency or "").strip() or None
    email = (email or "").strip().lower() or None
    if not any((name, agency, email)):
        return []

    statement = select(Contact)
    clauses = []
    if name:
        clauses.append(_contains(Contact.name, name))
    if agency:
        clauses.append(_contains(Contact.agency_path, agency))
    if email:
        clauses.append(or_(_contains(Contact.email, email), Contact.email == email))
    if len(clauses) == 1:
        statement = statement.where(clauses[0])
    else:
        statement = statement.where(or_(*clauses))

    rows = session.scalars(
        statement.order_by(Contact.id.asc()).limit(limit)
    ).all()
    return [
        ContactRecord(
            id=row.id,
            name=row.name,
            email=row.email,
            phone=row.phone,
            title=row.title,
            agency_path=row.agency_path,
            contact_type=row.contact_type,
            first_seen_opportunity_id=row.first_seen_opportunity_id,
        )
        for row in rows
    ]
