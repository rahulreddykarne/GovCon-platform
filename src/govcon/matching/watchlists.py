"""Watchlist CRUD helpers for Phase 2."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.models import Watchlist

WATCHLIST_ARRAY_FIELDS = (
    "psc_codes",
    "naics_codes",
    "keywords",
    "exclude_keywords",
    "nsn_list",
    "set_asides",
    "sources",
)


def _normalize_array(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    cleaned = [item.strip() for item in value if item and item.strip()]
    return cleaned


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
    max_value: Decimal | None = None,
    min_value: Decimal | None = None,
    min_deadline_days: int | None = None,
    sources: list[str] | None = None,
    notes: str | None = None,
) -> Watchlist:
    row = Watchlist(
        name=name.strip(),
        enabled=enabled,
        psc_codes=_normalize_array(psc_codes),
        naics_codes=_normalize_array(naics_codes),
        keywords=_normalize_array(keywords),
        exclude_keywords=_normalize_array(exclude_keywords),
        nsn_list=_normalize_array(nsn_list),
        set_asides=_normalize_array(set_asides),
        max_value=max_value,
        min_value=min_value,
        min_deadline_days=min_deadline_days,
        sources=_normalize_array(sources),
        notes=notes,
    )
    session.add(row)
    session.flush()
    return row


def list_watchlists(session: Session, *, include_disabled: bool = True) -> list[Watchlist]:
    query = select(Watchlist).order_by(Watchlist.id)
    if not include_disabled:
        query = query.where(Watchlist.enabled.is_(True))
    return list(session.scalars(query).all())


def get_watchlist(session: Session, watchlist_id: int) -> Watchlist | None:
    return session.get(Watchlist, watchlist_id)


def update_watchlist(session: Session, watchlist: Watchlist, **fields: Any) -> Watchlist:
    for key, value in fields.items():
        if key not in WATCHLIST_ARRAY_FIELDS and not hasattr(watchlist, key):
            raise ValueError(f"unknown watchlist field: {key}")
        if key in WATCHLIST_ARRAY_FIELDS:
            value = _normalize_array(value)
        setattr(watchlist, key, value)
    session.flush()
    return watchlist


def disable_watchlist(session: Session, watchlist: Watchlist) -> Watchlist:
    watchlist.enabled = False
    session.flush()
    return watchlist
