"""Idempotent local demo data.

Targeting codes are not hardcoded. The demo watchlist is an empty container
the operator fills in.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from govcon.models import Watchlist

DEMO_WATCHLIST_NAME = "Demo Watchlist"
DEMO_WATCHLIST_NOTES = (
    "Seeded by `govcon db seed-demo-watchlist` for local development. "
    "The application does not hardcode product categories. "
    "Replace the empty PSC, NAICS, NSN, keyword, and set-aside arrays with your targeting."
)


def seed_demo_watchlist(session: Session) -> tuple[Watchlist, bool]:
    """Insert the demo watchlist when missing. Returns ``(row, created)``."""
    existing = session.scalar(select(Watchlist).where(Watchlist.name == DEMO_WATCHLIST_NAME))
    if existing is not None:
        return existing, False
    row = Watchlist(
        name=DEMO_WATCHLIST_NAME,
        enabled=True,
        psc_codes=[],
        naics_codes=[],
        keywords=[],
        exclude_keywords=[],
        nsn_list=[],
        set_asides=[],
        sources=["sam", "dibbs", "usaspending"],
        notes=DEMO_WATCHLIST_NOTES,
    )
    session.add(row)
    session.flush()
    return row, True


def demo_watchlist_count(session: Session) -> int:
    return int(
        session.scalar(
            select(func.count()).select_from(Watchlist).where(Watchlist.name == DEMO_WATCHLIST_NAME)
        )
        or 0
    )
