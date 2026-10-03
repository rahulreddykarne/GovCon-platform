"""Close opportunities whose response deadline has passed, for every source.

DIBBS index rows are always ingested ``open`` and SAM keeps notices active
after their deadline, so without this sweep expired rows stay ``open``
forever. The sweep does not call any source and writes no snapshot (no new
payload was fetched); it records a ``status_changed`` event, which is not a
material source change. A later ingest with a new deadline sets the row back
to the status the source reports.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.ingest.runs import IngestStats
from govcon.models import Opportunity, OpportunityEvent

CLOSED_STATUS = "closed"


def close_expired_opportunities(session: Session, *, now: datetime | None = None) -> IngestStats:
    """Mark ``open`` rows whose ``response_deadline`` is before ``now`` as ``closed``."""
    now = now or datetime.now(UTC)
    rows = session.scalars(
        select(Opportunity).where(
            Opportunity.status == "open",
            Opportunity.response_deadline.is_not(None),
            Opportunity.response_deadline < now,
        )
    ).all()
    stats = IngestStats(fetched=len(rows))
    for row in rows:
        previous = row.status
        row.status = CLOSED_STATUS
        session.add(
            OpportunityEvent(
                opportunity_id=row.id,
                event_type="status_changed",
                field_name="status",
                old_value={"value": previous},
                new_value={"value": CLOSED_STATUS, "reason": "response_deadline_passed"},
                snapshot_id=None,
            )
        )
        stats.updated += 1
    session.flush()
    return stats
