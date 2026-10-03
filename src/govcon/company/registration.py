"""Daily refresh of our own SAM registration and its expiry alert (roadmap gap 7, ADR-072).

Only the registration fields SAM is authoritative for are refreshed: legal
name, CAGE code, registration status and expiration date. All other company
facts stay human-maintained in ``COMPANY_FACTS_PATH``. Refreshed values carry
their source and time; values older than ``COMPANY_FACTS_MAX_AGE_DAYS`` are
treated as unknown rather than current.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.config import Settings, get_settings
from govcon.models import CompanyRegistration, User

SYNCED_FIELDS = ("legal_name", "cage_code", "sam_registration_status", "sam_expiration_date")


@dataclass
class RefreshResult:
    status: str  # refreshed | skipped
    reason: str | None = None
    expiration_date: date | None = None
    alerted: bool = False


def company_uei(settings: Settings, facts: dict[str, Any] | None = None) -> str | None:
    uei = settings.company_uei or (facts or {}).get("uei")
    return uei.strip().upper() if isinstance(uei, str) and uei.strip() else None


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def refresh_company_registration(session: Session, *, settings: Settings | None = None, client=None,
                                 now: datetime | None = None) -> RefreshResult:
    """Fetch our registration from SAM, store it, and alert ahead of expiry."""
    from govcon.compliance.pipeline import read_company_facts_file
    from govcon.ingest.sam_entities import ensure_vendor

    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    uei = company_uei(settings, read_company_facts_file(settings))
    if uei is None:
        return RefreshResult("skipped", "no company UEI (set COMPANY_UEI or uei in the company facts file)")
    if not settings.sam_api_key:
        return RefreshResult("skipped", "SAM_API_KEY is not set")
    vendor, _ = ensure_vendor(session, uei, refresh=True, client=client, settings=settings, now=now)
    raw = vendor.raw or {}
    registration = raw.get("entityRegistration") or {}
    row = session.get(CompanyRegistration, uei)
    if row is None:
        row = CompanyRegistration(uei=uei, refreshed_at=now)
        session.add(row)
    row.legal_name = vendor.legal_name or registration.get("legalBusinessName")
    row.cage_code = vendor.cage_code or registration.get("cageCode")
    row.registration_status = vendor.registration_status or registration.get("registrationStatus")
    row.expiration_date = _parse_date(registration.get("registrationExpirationDate"))
    row.source = "sam_entity_api"
    row.refreshed_at = now
    row.raw = raw
    session.flush()
    return RefreshResult("refreshed", expiration_date=row.expiration_date,
                         alerted=_alert_if_expiring(session, row, settings, now))


def _alert_if_expiring(session: Session, row: CompanyRegistration, settings: Settings, now: datetime) -> bool:
    """Alert owners and approvers once a week from ``SAM_EXPIRY_ALERT_DAYS`` before expiry."""
    from govcon.collaboration.notifications import notify

    if row.expiration_date is None:
        return False
    days_left = (row.expiration_date - now.date()).days
    if days_left > settings.sam_expiry_alert_days:
        return False
    if row.last_expiry_alert_at is not None and now - row.last_expiry_alert_at < timedelta(days=7):
        return False
    for user_id in session.scalars(select(User.id).where(User.is_active.is_(True), User.role.in_(("owner", "approver")))):
        notify(session, user_id=user_id, notification_type="registration_expiring", settings=settings,
               payload={"uei": row.uei, "expiration_date": row.expiration_date.isoformat(), "days_left": days_left})
    row.last_expiry_alert_at = now
    session.flush()
    return True


def overlay_registration(session: Session, facts: dict[str, Any], *, settings: Settings | None = None,
                         now: datetime | None = None) -> dict[str, Any]:
    """Company facts with SAM-sourced registration fields applied, and their provenance.

    A fresh refresh replaces the registration fields; a stale one removes them
    so they read as unknown. Other facts are returned unchanged.
    """
    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    uei = company_uei(settings, facts)
    row = session.get(CompanyRegistration, uei) if uei else None
    if row is None:
        return facts
    merged = dict(facts)
    provenance = dict(merged.get("_provenance") or {})
    age = now - row.refreshed_at
    if age > timedelta(days=settings.company_facts_max_age_days):
        for name in ("sam_registration_status", "sam_expiration_date"):
            merged.pop(name, None)
        provenance["sam_registration"] = {
            "source": row.source, "refreshed_at": row.refreshed_at.isoformat(), "stale": True,
            "note": f"SAM registration data is {age.days} days old; treated as unknown until refreshed",
        }
    else:
        values = {
            "legal_name": row.legal_name,
            "cage_code": row.cage_code,
            "sam_registration_status": row.registration_status,
            "sam_expiration_date": row.expiration_date.isoformat() if row.expiration_date else None,
        }
        merged.update({name: value for name, value in values.items() if value is not None})
        provenance["sam_registration"] = {"source": row.source, "refreshed_at": row.refreshed_at.isoformat(),
                                          "stale": False, "fields": [n for n, v in values.items() if v is not None]}
    merged["_provenance"] = provenance
    return merged
