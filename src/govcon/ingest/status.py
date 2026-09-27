"""Actionable opportunity status derived from source facts and the current time."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.ingest.runs import IngestStats
from govcon.models import Opportunity, OpportunityEvent

_SAM_BID_TYPES = frozenset({"solicitation", "combined synopsis/solicitation"})


def opportunity_status(
    *,
    source: str,
    notice_type: str | None,
    active: str | None,
    response_deadline: datetime | None,
    archive_date: date | None,
    now: datetime | None = None,
) -> str:
    """Classify records without presenting awards or expired bids as open RFQs."""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")
    kind = (notice_type or "").strip().lower()
    if "cancel" in kind:
        return "cancelled"
    if source == "sam" and kind == "award notice":
        return "awarded"
    if source == "sam" and kind not in _SAM_BID_TYPES:
        return "informational"
    if (active or "").strip().lower() in {"no", "false"}:
        return "archived"
    if archive_date is not None and archive_date < now.date():
        return "archived"
    if response_deadline is not None:
        deadline = response_deadline
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        if deadline <= now:
            return "closed"
    return "open"


def refresh_opportunity_statuses(session: Session, *, now: datetime | None = None) -> IngestStats:
    """Reclassify stored source records without changing their raw snapshots."""
    now = now or datetime.now(UTC)
    stats = IngestStats()
    rows = session.scalars(
        select(Opportunity).where(Opportunity.source.in_(("sam", "dibbs")))
    ).all()
    for row in rows:
        raw = row.raw if isinstance(row.raw, dict) else {}
        cancelled = any(
            "cancel" in str(raw.get(key, "")).lower()
            for key in ("type", "baseType", "archiveType", "status")
        )
        new_status = opportunity_status(
            source=row.source,
            notice_type="cancelled" if cancelled else row.opportunity_type,
            active=str(raw.get("active")) if raw.get("active") is not None else None,
            response_deadline=row.response_deadline,
            archive_date=row.archive_date,
            now=now,
        )
        stats.fetched += 1
        if new_status == row.status:
            stats.unchanged += 1
            continue
        previous = row.status
        row.status = new_status
        session.add(OpportunityEvent(
            opportunity_id=row.id,
            event_type="status_changed",
            field_name="status",
            old_value={"value": previous},
            new_value={"value": new_status},
            snapshot_id=None,
        ))
        stats.updated += 1
    return stats
