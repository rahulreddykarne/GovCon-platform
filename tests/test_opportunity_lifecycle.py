"""M3: open rows of every source close once their response deadline passes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.ingest.lifecycle import close_expired_opportunities
from govcon.models import Opportunity, OpportunityEvent

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


@pytest.fixture()
def session(upgraded_engine):
    with Session(upgraded_engine) as s:
        yield s
        s.rollback()


def _opp(session: Session, *, source: str = "dibbs", status: str = "open", deadline: datetime | None) -> Opportunity:
    row = Opportunity(
        source=source,
        source_id=f"lifecycle-{uuid4()}",
        title="Lifecycle fixture",
        status=status,
        response_deadline=deadline,
        raw={"fixture": True},
        links={},
    )
    session.add(row)
    session.flush()
    return row


def test_expired_open_rows_close_for_every_source(session: Session) -> None:
    expired_dibbs = _opp(session, deadline=NOW - timedelta(minutes=1))
    expired_sam = _opp(session, source="sam", deadline=NOW - timedelta(days=3))
    future = _opp(session, deadline=NOW + timedelta(hours=1))
    no_deadline = _opp(session, deadline=None)
    cancelled = _opp(session, status="cancelled", deadline=NOW - timedelta(days=1))

    stats = close_expired_opportunities(session, now=NOW)
    assert stats.updated >= 2
    for row in (expired_dibbs, expired_sam):
        assert row.status == "closed"
        event = session.scalar(
            select(OpportunityEvent).where(
                OpportunityEvent.opportunity_id == row.id, OpportunityEvent.event_type == "status_changed"
            )
        )
        assert event is not None and event.old_value == {"value": "open"}
        assert event.new_value["value"] == "closed" and event.snapshot_id is None
    assert future.status == "open" and no_deadline.status == "open"
    assert cancelled.status == "cancelled"

    # Idempotent: nothing left to close, and no duplicate events.
    again = close_expired_opportunities(session, now=NOW)
    assert again.updated == 0
    events = session.scalars(
        select(OpportunityEvent).where(
            OpportunityEvent.opportunity_id == expired_dibbs.id, OpportunityEvent.event_type == "status_changed"
        )
    ).all()
    assert len(events) == 1
