"""Immutable opportunity snapshots and field-diff events.

A new snapshot is written only when the canonical payload hash changes.
Field events are inserted before the current ``opportunities`` row is updated.

Snapshots are unique per content hash. When the source reverts to content seen
before (A -> B -> A), the live row is still updated and the field events link
to the existing snapshot for that content; no duplicate snapshot is written.
The current snapshot is the one whose hash equals ``opportunities.raw_hash``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.models import Contact, Opportunity, OpportunityEvent, OpportunitySnapshot
from govcon.workflow.source_events import (
    MATERIAL_EVENT_TYPES,
    MATERIAL_SOURCE_CHANGE_EVENT,
    change_level,
)

SOURCE_SAM = "sam"


@dataclass(frozen=True)
class NormalizedOpportunity:
    source: str
    source_id: str
    solicitation_number: str | None
    title: str | None
    description: str | None
    description_hash: str
    opportunity_type: str | None
    psc_code: str | None
    naics_code: str | None
    set_aside_code: str | None
    agency_path: str | None
    place_of_performance: dict | None
    nsn: str | None
    nsn_candidates: tuple[str, ...]
    quantity: Decimal | None
    unit: str | None
    estimated_value_min: Decimal | None
    estimated_value_max: Decimal | None
    estimated_value_source: str | None
    posted_date: date | None
    response_deadline: datetime | None
    archive_date: date | None
    status: str
    poc: list | None
    links: dict
    raw: dict
    content_hash: str
    source_version: str | None
    contacts: tuple[dict, ...]


def canonical_content_hash(payload: dict) -> str:
    """SHA-256 of canonical JSON. Key order does not change the hash."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def description_hash(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def _event_value(value: Any) -> dict:
    return {"value": json_safe(value)}


def _same(left: Any, right: Any) -> bool:
    return json_safe(left) == json_safe(right)


def _award_present(raw: dict | None) -> bool:
    if not isinstance(raw, dict):
        return False
    award = raw.get("award")
    if not isinstance(award, dict):
        return False
    return any(award.get(key) not in (None, "", "null") for key in ("date", "number", "amount"))


def _attachment_urls(links: dict | None) -> list[str]:
    if not isinstance(links, dict):
        return []
    attachments = links.get("attachments") or []
    if not isinstance(attachments, list):
        return []
    return [item for item in attachments if isinstance(item, str)]


def _links_without_attachments(links: dict | None) -> dict:
    if not isinstance(links, dict):
        return {}
    return {key: value for key, value in links.items() if key != "attachments"}


def normalized_document(item: NormalizedOpportunity) -> dict:
    return json_safe(
        {
            "source": item.source,
            "source_id": item.source_id,
            "solicitation_number": item.solicitation_number,
            "title": item.title,
            "description": item.description,
            "description_hash": item.description_hash,
            "opportunity_type": item.opportunity_type,
            "psc_code": item.psc_code,
            "naics_code": item.naics_code,
            "set_aside_code": item.set_aside_code,
            "agency_path": item.agency_path,
            "place_of_performance": item.place_of_performance,
            "nsn": item.nsn,
            "nsn_candidates": list(item.nsn_candidates),
            "quantity": item.quantity,
            "unit": item.unit,
            "estimated_value_min": item.estimated_value_min,
            "estimated_value_max": item.estimated_value_max,
            "estimated_value_source": item.estimated_value_source,
            "posted_date": item.posted_date,
            "response_deadline": item.response_deadline,
            "archive_date": item.archive_date,
            "status": item.status,
            "poc": item.poc,
            "links": item.links,
            "contacts": list(item.contacts),
        }
    )


def _tracked_events(existing: Opportunity, item: NormalizedOpportunity) -> list[OpportunityEvent]:
    events: list[OpportunityEvent] = []
    comparisons = (
        ("response_deadline", existing.response_deadline, item.response_deadline, "deadline_changed"),
        ("set_aside_code", existing.set_aside_code, item.set_aside_code, "set_aside_changed"),
        ("status", existing.status, item.status, "status_changed"),
        ("title", existing.title, item.title, "title_changed"),
        ("quantity", existing.quantity, item.quantity, "quantity_changed"),
        ("estimated_value_min", existing.estimated_value_min, item.estimated_value_min, "estimated_value_changed"),
        ("estimated_value_max", existing.estimated_value_max, item.estimated_value_max, "estimated_value_changed"),
    )
    for field_name, old, new, event_type in comparisons:
        if _same(old, new):
            continue
        events.append(
            OpportunityEvent(
                opportunity_id=existing.id,
                event_type=event_type,
                field_name=field_name,
                old_value=_event_value(old),
                new_value=_event_value(new),
            )
        )
        if field_name == "status" and new == "cancelled" and old != "cancelled":
            events.append(
                OpportunityEvent(
                    opportunity_id=existing.id,
                    event_type="cancelled",
                    field_name="status",
                    old_value=_event_value(old),
                    new_value=_event_value(new),
                )
            )

    old_description_hash = description_hash(existing.description)
    if old_description_hash != item.description_hash:
        events.append(
            OpportunityEvent(
                opportunity_id=existing.id,
                event_type="description_changed",
                field_name="description_hash",
                old_value=_event_value(old_description_hash),
                new_value=_event_value(item.description_hash),
            )
        )

    old_attachments = set(_attachment_urls(existing.links))
    new_attachments = set(_attachment_urls(item.links))
    added = sorted(new_attachments - old_attachments)
    removed = sorted(old_attachments - new_attachments)
    if added:
        events.append(
            OpportunityEvent(
                opportunity_id=existing.id,
                event_type="files_added",
                field_name="links",
                old_value=_event_value(sorted(old_attachments)),
                new_value=_event_value(added),
            )
        )
    if removed:
        events.append(
            OpportunityEvent(
                opportunity_id=existing.id,
                event_type="files_removed",
                field_name="links",
                old_value=_event_value(removed),
                new_value=_event_value(sorted(new_attachments)),
            )
        )
    if not _same(_links_without_attachments(existing.links), _links_without_attachments(item.links)):
        events.append(
            OpportunityEvent(
                opportunity_id=existing.id,
                event_type="links_changed",
                field_name="links",
                old_value=_event_value(_links_without_attachments(existing.links)),
                new_value=_event_value(_links_without_attachments(item.links)),
            )
        )

    if _award_present(item.raw) and not _award_present(existing.raw if isinstance(existing.raw, dict) else None):
        events.append(
            OpportunityEvent(
                opportunity_id=existing.id,
                event_type="awarded",
                field_name="award",
                old_value=_event_value(None),
                new_value=_event_value(item.raw.get("award")),
            )
        )
    return events


def _apply_current(row: Opportunity, item: NormalizedOpportunity) -> None:
    row.solicitation_number = item.solicitation_number
    row.title = item.title
    row.description = item.description
    row.opportunity_type = item.opportunity_type
    row.psc_code = item.psc_code
    row.naics_code = item.naics_code
    row.set_aside_code = item.set_aside_code
    row.agency_path = item.agency_path
    row.place_of_performance = item.place_of_performance
    row.nsn = item.nsn
    row.nsn_candidates = list(item.nsn_candidates) or None
    row.quantity = item.quantity
    row.unit = item.unit
    row.estimated_value_min = item.estimated_value_min
    row.estimated_value_max = item.estimated_value_max
    row.estimated_value_source = item.estimated_value_source
    row.posted_date = item.posted_date
    row.response_deadline = item.response_deadline
    row.archive_date = item.archive_date
    row.status = item.status
    row.poc = item.poc
    row.links = item.links
    row.raw = item.raw
    row.raw_hash = item.content_hash


def current_snapshot_id(session: Session, opportunity_id: int) -> int | None:
    """Snapshot of the content the live row holds, else the most recently fetched one."""
    current = session.scalar(
        select(OpportunitySnapshot.id)
        .join(Opportunity, Opportunity.id == OpportunitySnapshot.opportunity_id)
        .where(
            OpportunitySnapshot.opportunity_id == opportunity_id,
            OpportunitySnapshot.content_hash == Opportunity.raw_hash,
        )
        .limit(1)
    )
    if current is not None:
        return current
    return session.scalar(
        select(OpportunitySnapshot.id)
        .where(OpportunitySnapshot.opportunity_id == opportunity_id)
        .order_by(OpportunitySnapshot.fetched_at.desc(), OpportunitySnapshot.id.desc())
        .limit(1)
    )


def _insert_snapshot(session: Session, opportunity_id: int, item: NormalizedOpportunity) -> OpportunitySnapshot:
    snapshot = OpportunitySnapshot(
        opportunity_id=opportunity_id,
        source_version=item.source_version,
        content_hash=item.content_hash,
        raw=item.raw,
        normalized=normalized_document(item),
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def _upsert_contacts(session: Session, opportunity_id: int, item: NormalizedOpportunity) -> None:
    if not item.agency_path:
        return
    # A notice may list one person twice (primary and secondary contact). The
    # session does not autoflush, so the query below cannot see a contact added
    # earlier in this loop; reuse it instead of inserting a duplicate.
    added: dict[str, Contact] = {}
    for contact in item.contacts:
        email = contact.get("email")
        if not isinstance(email, str) or "@" not in email:
            continue
        normalized_email = email.strip().lower()
        existing = added.get(normalized_email) or session.scalar(
            select(Contact).where(Contact.email == normalized_email, Contact.agency_path == item.agency_path)
        )
        if existing is None:
            added[normalized_email] = Contact(
                name=contact.get("name"),
                email=normalized_email,
                phone=contact.get("phone"),
                title=contact.get("title"),
                agency_path=item.agency_path,
                contact_type=contact.get("contact_type"),
                first_seen_opportunity_id=opportunity_id,
            )
            session.add(added[normalized_email])
            continue
        if contact.get("name"):
            existing.name = contact["name"]
        if contact.get("phone"):
            existing.phone = contact["phone"]
        if contact.get("title"):
            existing.title = contact["title"]
        if contact.get("contact_type"):
            existing.contact_type = contact["contact_type"]


def upsert_opportunity(session: Session, item: NormalizedOpportunity) -> str:
    """Insert or update one opportunity. Returns inserted, updated, or unchanged."""
    existing = session.scalar(
        select(Opportunity).where(Opportunity.source == item.source, Opportunity.source_id == item.source_id)
    )
    if existing is None:
        row = Opportunity(
            source=item.source,
            source_id=item.source_id,
            raw=item.raw,
            raw_hash=item.content_hash,
            status=item.status,
        )
        _apply_current(row, item)
        session.add(row)
        session.flush()
        created_snapshot = _insert_snapshot(session, row.id, item)
        session.add(
            OpportunityEvent(
                opportunity_id=row.id,
                event_type="created",
                field_name=None,
                old_value=None,
                new_value=_event_value(item.source_id),
                snapshot_id=created_snapshot.id,
            )
        )
        _upsert_contacts(session, row.id, item)
        return "inserted"

    if existing.raw_hash == item.content_hash:
        return "unchanged"
    # Content seen before (a revert, A -> B -> A) is still an update to the live
    # row; it reuses the stored snapshot for that content.
    snapshot = session.scalar(
        select(OpportunitySnapshot).where(
            OpportunitySnapshot.opportunity_id == existing.id,
            OpportunitySnapshot.content_hash == item.content_hash,
        )
    )

    events = _tracked_events(existing, item)
    level = change_level({event.event_type for event in events})
    if level is not None:
        # One marker per material update; the source-change workflow consumes it.
        events.append(
            OpportunityEvent(
                opportunity_id=existing.id,
                event_type=MATERIAL_SOURCE_CHANGE_EVENT,
                field_name=None,
                old_value=None,
                new_value={
                    "value": {
                        "level": level,
                        "event_types": sorted(
                            {e.event_type for e in events if e.event_type in MATERIAL_EVENT_TYPES}
                        ),
                    }
                },
            )
        )
    if snapshot is None:
        snapshot = _insert_snapshot(session, existing.id, item)
    for event in events:
        event.snapshot_id = snapshot.id
        session.add(event)
    session.flush()
    _apply_current(existing, item)
    _upsert_contacts(session, existing.id, item)
    return "updated"
