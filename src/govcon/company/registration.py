"""Daily refresh of our own SAM registration and its expiry alert (roadmap gap 7, ADR-072).

Only the registration fields SAM is authoritative for are refreshed: legal
name, CAGE code, registration status and expiration date. All other company
facts stay human-maintained in ``COMPANY_FACTS_PATH``. A refresh publishes
``fetched_at``, ``refreshed_at``, ``source_updated_at``, ``expires_at`` and a
freshness status. Missing, stale, failed, or superseded values become unknown
instead of leaving an older registration's assertions in place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.config import Settings, get_settings
from govcon.ingest import sam_entities
from govcon.ingest.freshness import (
    accept_payload,
    cancel_attempt,
    claim_attempt,
    clear_company_assertions,
    commit_freshness,
    evidence_status,
    mark_failed,
    registration_key,
    replace_registration,
    safe_error,
)
from govcon.ingest.sam_entities import SamEntityError
from govcon.models import CompanyRegistration, User, Vendor

SYNCED_FIELDS = ("legal_name", "cage_code", "sam_registration_status", "sam_expiration_date")


@dataclass
class RefreshResult:
    status: str  # refreshed | skipped | failed | discarded
    reason: str | None = None
    expiration_date: date | None = None
    alerted: bool = False
    attempt_id: str | None = None


def company_uei(settings: Settings, facts: dict[str, Any] | None = None) -> str | None:
    uei = settings.company_uei or (facts or {}).get("uei")
    return uei.strip().upper() if isinstance(uei, str) and uei.strip() else None


def _load(session: Session, uei: str) -> CompanyRegistration | None:
    row = session.get(CompanyRegistration, uei)
    if row is not None:
        session.refresh(row)
    return row


def refresh_company_registration(session: Session, *, settings: Settings | None = None, client=None,
                                 now: datetime | None = None) -> RefreshResult:
    """Fetch our registration from SAM and store it, including a failed attempt.

    The vendor cache and this row share the caller's transaction. A failed
    fetch clears assertions here and on the vendor row; it does not raise, so
    the caller can commit the failure instead of rolling back to the previous
    registration.
    """
    from govcon.compliance.pipeline import read_company_facts_file

    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    uei = company_uei(settings, read_company_facts_file(settings))
    if uei is None:
        return RefreshResult("skipped", "no company UEI (set COMPANY_UEI or uei in the company facts file)")
    if not settings.sam_api_key:
        return RefreshResult("skipped", "SAM_API_KEY is not set")
    row = _load(session, uei)
    if row is None:
        row = CompanyRegistration(uei=uei, refreshed_at=now, source="sam_entity_api")
        session.add(row)
    attempt_id = claim_attempt(row, now)
    session.flush()
    try:
        vendor, applied = sam_entities.ensure_vendor(
            session, uei, refresh=True, client=client, settings=settings, now=now,
        )
    except SamEntityError as exc:
        current = _load(session, uei) or row
        mark_failed(current, attempt_id, safe_error(exc, settings), now, kind="company")
        session.flush()
        session.expire(current)
        return RefreshResult("failed", safe_error(exc, settings), attempt_id=attempt_id)
    return _publish_company(session, uei, attempt_id, vendor, applied, now, settings)


def _publish_company(
    session: Session,
    uei: str,
    attempt_id: str,
    vendor: Vendor,
    vendor_applied: bool,
    now: datetime,
    settings: Settings,
) -> RefreshResult:
    row = _load(session, uei)
    if row is None:
        return RefreshResult("failed", "company registration row disappeared before publish", attempt_id=attempt_id)
    raw = vendor.raw if isinstance(getattr(vendor, "raw", None), dict) else None
    source_updated = getattr(vendor, "source_updated_at", None)
    expires = getattr(vendor, "expires_at", None)
    if expires is None and raw is not None:
        from govcon.ingest.freshness import expiration_date

        expires = expiration_date(raw)
    if not vendor_applied or not accept_payload(row, attempt_id, now=now, source_updated=source_updated):
        session.flush()
        session.expire(row)
        return RefreshResult("discarded", "older or superseded SAM response was not applied", attempt_id=attempt_id)
    key = getattr(vendor, "registration_key", None) or registration_key(uei, raw)
    replace_registration(row, key=key, kind="company")
    # Nulls are written on purpose: a fresh unknown must not keep the file's or
    # the previous refresh's assertion.
    row.legal_name = getattr(vendor, "legal_name", None)
    row.cage_code = getattr(vendor, "cage_code", None)
    row.registration_status = getattr(vendor, "registration_status", None)
    row.source = "sam_entity_api"
    row.raw = raw
    commit_freshness(row, now=now, source_updated=source_updated, expires=expires, kind="company")
    session.flush()
    alerted = _alert_if_expiring(session, row, settings, now)
    session.expire(row)
    stored = _load(session, uei)
    return RefreshResult(
        "refreshed",
        expiration_date=stored.expiration_date if stored is not None else row.expiration_date,
        alerted=alerted,
        attempt_id=attempt_id,
    )


def cancel_company_refresh(session: Session, uei: str, attempt_id: str) -> bool:
    """Cancel an in-flight company refresh. A late payload with this attempt is ignored."""
    row = _load(session, uei)
    if row is None:
        return False
    cancelled = cancel_attempt(row, attempt_id)
    if cancelled:
        session.flush()
        session.expire(row)
    return cancelled


def _alert_if_expiring(session: Session, row: CompanyRegistration, settings: Settings, now: datetime) -> bool:
    """Alert owners and approvers once a week from ``SAM_EXPIRY_ALERT_DAYS`` before expiry."""
    from govcon.collaboration.notifications import notify

    if row.freshness_status not in {"fresh", "expired"} or row.expiration_date is None:
        return False
    days_left = (row.expiration_date - now.date()).days
    if days_left > settings.sam_expiry_alert_days:
        return False
    if row.last_expiry_alert_at is not None and now - row.last_expiry_alert_at < timedelta(days=7):
        return False
    for user_id in session.scalars(select(User.id).where(User.is_active.is_(True), User.role.in_(("owner", "approver")))):
        notify(session, user_id=user_id, notification_type="registration_expiring", settings=settings,
               payload={"uei": row.uei, "expiration_date": row.expiration_date.isoformat(), "days_left": days_left,
                        "freshness_status": row.freshness_status})
    row.last_expiry_alert_at = now
    session.flush()
    return True


def overlay_registration(session: Session, facts: dict[str, Any], *, settings: Settings | None = None,
                         now: datetime | None = None) -> dict[str, Any]:
    """Company facts with SAM-sourced registration fields applied, and their provenance.

    A current refresh replaces each SAM field, and a null field removes the
    file's older assertion. Stale, failed, and unknown refreshes remove the
    SAM fields so they read as unknown.
    """
    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    uei = company_uei(settings, facts)
    row = _load(session, uei) if uei else None
    if row is None:
        return facts
    status = evidence_status(row, now=now, max_age=timedelta(days=settings.company_facts_max_age_days))
    if status != row.freshness_status and row.attempt_state != "in_progress":
        row.freshness_status = status
        if status in {"stale", "unknown", "failed"}:
            clear_company_assertions(row)
        session.flush()
    merged = dict(facts)
    provenance = dict(merged.get("_provenance") or {})
    values = {
        "legal_name": row.legal_name,
        "cage_code": row.cage_code,
        "sam_registration_status": row.registration_status,
        "sam_expiration_date": row.expiration_date.isoformat() if row.expiration_date else None,
    }
    if status in {"stale", "failed", "unknown"}:
        for name in SYNCED_FIELDS:
            merged.pop(name, None)
        provenance["sam_registration"] = {
            "source": row.source,
            "refreshed_at": row.refreshed_at.isoformat() if row.refreshed_at else None,
            "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None,
            "source_updated_at": row.source_updated_at.isoformat() if row.source_updated_at else None,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            "freshness_status": status,
            "stale": True,
            "note": "SAM registration data is not current; treated as unknown until a successful refresh",
        }
    else:
        for name in SYNCED_FIELDS:
            value = values[name]
            if value is None:
                merged.pop(name, None)
            else:
                merged[name] = value
        provenance["sam_registration"] = {
            "source": row.source,
            "refreshed_at": row.refreshed_at.isoformat() if row.refreshed_at else None,
            "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None,
            "source_updated_at": row.source_updated_at.isoformat() if row.source_updated_at else None,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            "freshness_status": status,
            "stale": False,
            "fields": [name for name in SYNCED_FIELDS if values[name] is not None],
        }
    merged["_provenance"] = provenance
    return merged
