"""Watchlist CRUD helpers."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.models import Watchlist


def list_watchlists(session: Session) -> list[Watchlist]:
    return list(session.scalars(select(Watchlist).order_by(Watchlist.id)).all())


def get_watchlist(session: Session, watchlist_id: int) -> Watchlist | None:
    return session.get(Watchlist, watchlist_id)


def create_watchlist(
    session: Session,
    *,
    name: str,
    enabled: bool = True,
    psc_codes: list[str] | None = None,
    naics_codes: list[str] | None = None,
    keywords: list[str] | None = None,
    exclude_keywords: list[str] | None = None,
    nsn_list: list[str] | None = None,
    set_asides: list[str] | None = None,
    sources: list[str] | None = None,
    min_value: Decimal | None = None,
    max_value: Decimal | None = None,
    min_deadline_days: int | None = None,
    notes: str | None = None,
) -> Watchlist:
    row = Watchlist(
        name=name.strip(),
        enabled=enabled,
        psc_codes=psc_codes or [],
        naics_codes=naics_codes or [],
        keywords=keywords or [],
        exclude_keywords=exclude_keywords or [],
        nsn_list=nsn_list or [],
        set_asides=set_asides or [],
        sources=sources or ["sam", "dibbs"],
        min_value=min_value,
        max_value=max_value,
        min_deadline_days=min_deadline_days,
        notes=notes,
    )
    session.add(row)
    session.flush()
    return row


def update_watchlist(session: Session, watchlist: Watchlist, **fields) -> Watchlist:
    for key, value in fields.items():
        if value is not None or key in {
            "min_value",
            "max_value",
            "min_deadline_days",
            "notes",
        }:
            setattr(watchlist, key, value)
    session.flush()
    return watchlist


def disable_watchlist(session: Session, watchlist: Watchlist) -> Watchlist:
    watchlist.enabled = False
    session.flush()
    return watchlist
