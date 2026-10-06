"""Idempotent local demo data.

Targeting codes are not hardcoded. The demo watchlist is an empty container
the operator fills in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from govcon.models import Opportunity, Watchlist

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


DEMO_NOTICE_NOTE = (
    "Sanitized local demo. This is not a government solicitation and states no real agency requirement."
)


def seed_demo_opportunities(session: Session) -> int:
    """Insert a few fictional notices. Safe to run more than once. Returns how many were added."""
    deadline = datetime.now(UTC) + timedelta(days=21)
    specs = (
        ("DEMO-0001", "Sanitized demo — valve kit", "The contractor shall package the demo item."),
        ("DEMO-0002", "Sanitized demo — bearing set", "DEMO ONLY. Quantity and delivery are unknown."),
        ("DEMO-0003", "Sanitized demo — filter element", "No clause text is included in this demo row."),
    )
    created = 0
    for source_id, title, description in specs:
        existing = session.scalar(
            select(Opportunity).where(Opportunity.source == "demo", Opportunity.source_id == source_id)
        )
        if existing is not None:
            continue
        session.add(Opportunity(
            source="demo",
            source_id=source_id,
            solicitation_number=source_id,
            title=title,
            description=f"{DEMO_NOTICE_NOTE} {description}",
            opportunity_type="demo",
            psc_code="ZZ99",
            naics_code=None,
            set_aside_code=None,
            agency_path="Demo agency (not a real office)",
            status="open",
            response_deadline=deadline,
            raw={"demo": True, "note": DEMO_NOTICE_NOTE},
            raw_hash=f"demo-{source_id}",
        ))
        created += 1
    session.flush()
    return created


def demo_watchlist_count(session: Session) -> int:
    return int(
        session.scalar(
            select(func.count()).select_from(Watchlist).where(Watchlist.name == DEMO_WATCHLIST_NAME)
        )
        or 0
    )
