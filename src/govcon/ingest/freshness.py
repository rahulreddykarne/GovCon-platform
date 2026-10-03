"""SAM registration freshness.

A stored registration is usable only with the time it was fetched, the time
the source said it was updated, when it expires, and an explicit freshness
status. A failed or cancelled refresh does not leave the previous
registration's assertions looking current, and a response that is older than
one already stored is discarded.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

from govcon.config import Settings, get_settings
from govcon.logging import redact

FRESHNESS_STATUSES = frozenset({"fresh", "stale", "expired", "failed", "unknown"})
# Positive eligibility may use only a fresh registration. Expired is still
# evidence: the registration is known not to be active. Everything else is unknown.
USABLE_STATUSES = frozenset({"fresh", "expired"})


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _as_utc(value)
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed_date = parse_date(text)
        if parsed_date is None:
            return None
        return datetime(parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=UTC)
    return _as_utc(parsed)


def entity_registration(raw: dict | None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    registration = raw.get("entityRegistration")
    return registration if isinstance(registration, dict) else {}


def source_updated_at(raw: dict | None) -> datetime | None:
    registration = entity_registration(raw)
    for key in ("lastUpdateDate", "registrationLastUpdateDate", "activationDate", "registrationDate"):
        parsed = parse_datetime(registration.get(key))
        if parsed is not None:
            return parsed
    return None


def expiration_date(raw: dict | None) -> date | None:
    return parse_date(entity_registration(raw).get("registrationExpirationDate"))


def registration_key(uei: str, raw: dict | None) -> str:
    """Identity of one SAM registration period.

    A cancelled registration or a later activation is a different key, so its
    assertions cannot be merged into the previous period.
    """
    registration = entity_registration(raw)
    parts = [
        uei.strip().upper(),
        str(registration.get("registrationDate") or ""),
        str(registration.get("activationDate") or ""),
        str(registration.get("registrationStatus") or ""),
        str(registration.get("ueiStatus") or ""),
    ]
    return "|".join(parts)


def safe_error(exc: BaseException, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    text = redact(str(exc) or exc.__class__.__name__, settings.secret_values())
    text = redact(text, [settings.sam_api_key] if settings.sam_api_key else None)
    return text[:500]


def is_within(moment: datetime | None, now: datetime, window: timedelta) -> bool:
    if moment is None or window.total_seconds() < 0:
        return False
    return _as_utc(moment) >= _as_utc(now) - window


def claim_attempt(row: Any, now: datetime) -> str:
    """Start a refresh attempt. A later claim supersedes this one."""
    attempt_id = uuid4().hex
    row.attempt_id = attempt_id
    row.attempt_state = "in_progress"
    row.last_attempt_at = now
    return attempt_id


def attempt_current(row: Any, attempt_id: str) -> bool:
    return row.attempt_id == attempt_id and row.attempt_state == "in_progress"


def cancel_attempt(row: Any, attempt_id: str) -> bool:
    """Drop an in-flight attempt so its response can no longer be applied.

    A previously applied registration is left in place. The cancelled attempt
    never wrote assertions, and a restarted attempt receives a new id.
    """
    if not attempt_current(row, attempt_id):
        return False
    row.attempt_state = "cancelled"
    row.attempt_id = None
    return True


def response_is_older(
    row: Any,
    *,
    incoming_source_updated_at: datetime | None,
    incoming_fetched_at: datetime,
) -> bool:
    """True when applying this payload would move the row backwards."""
    stored_source = getattr(row, "source_updated_at", None)
    if stored_source is not None:
        stored_source = _as_utc(stored_source)
    if stored_source is not None and incoming_source_updated_at is not None:
        if incoming_source_updated_at < stored_source:
            return True
        if incoming_source_updated_at > stored_source:
            return False
    elif stored_source is not None and incoming_source_updated_at is None:
        return True
    stored_fetched = getattr(row, "fetched_at", None)
    return bool(stored_fetched is not None and _as_utc(incoming_fetched_at) < _as_utc(stored_fetched))


def release_stale_attempt(row: Any, attempt_id: str) -> None:
    if row.attempt_id == attempt_id and row.attempt_state == "in_progress":
        row.attempt_id = None
        row.attempt_state = None


def clear_vendor_assertions(row: Any) -> None:
    row.legal_name = None
    row.dba_name = None
    row.cage_code = None
    row.registration_status = None
    row.physical_address = None
    row.business_types = None
    row.naics_codes = None
    row.psc_codes = None
    row.points_of_contact = None
    row.raw = None
    row.registration_key = None
    row.expires_at = None


def clear_company_assertions(row: Any) -> None:
    row.legal_name = None
    row.cage_code = None
    row.registration_status = None
    row.expiration_date = None
    row.expires_at = None
    row.raw = None
    row.registration_key = None


def mark_failed(row: Any, attempt_id: str, message: str, now: datetime, *, kind: str) -> bool:
    """Record a failed refresh and drop assertions from the attempt's registration.

    A superseded or cancelled attempt is left untouched: its failure must not
    erase a newer registration, and it must not publish its own assertions.
    """
    if not attempt_current(row, attempt_id):
        return False
    if kind == "vendor":
        clear_vendor_assertions(row)
        row.last_refresh_error = message
    else:
        clear_company_assertions(row)
        row.last_error = message
    row.freshness_status = "failed"
    row.last_attempt_at = now
    row.attempt_state = "failed"
    row.attempt_id = None
    row.refresh_generation = int(row.refresh_generation or 0) + 1
    return True


def accept_payload(
    row: Any,
    attempt_id: str,
    *,
    now: datetime,
    source_updated: datetime | None,
) -> bool:
    """Whether this attempt may publish. An older or superseded response may not."""
    if not attempt_current(row, attempt_id):
        return False
    if response_is_older(row, incoming_source_updated_at=source_updated, incoming_fetched_at=now):
        release_stale_attempt(row, attempt_id)
        return False
    return True


def replace_registration(row: Any, *, key: str, kind: str) -> None:
    """Drop the previous registration's assertions when the registration changes.

    Call this before writing the new payload. A cancel or a restarted
    registration must not keep the earlier period's status, dates, or SAM
    assertion lists.
    """
    if row.registration_key and key != row.registration_key:
        if kind == "vendor":
            clear_vendor_assertions(row)
        else:
            clear_company_assertions(row)
    row.registration_key = key


def commit_freshness(
    row: Any,
    *,
    now: datetime,
    source_updated: datetime | None,
    expires: date | None,
    kind: str,
) -> None:
    row.source_updated_at = source_updated
    row.expires_at = expires
    row.fetched_at = now
    if kind == "company":
        row.expiration_date = expires
        row.refreshed_at = now
    row.freshness_status = "expired" if expires is not None and expires < now.date() else "fresh"
    row.last_attempt_at = now
    if kind == "vendor":
        row.last_refresh_error = None
    else:
        row.last_error = None
    row.attempt_state = "applied"
    row.attempt_id = None
    row.refresh_generation = int(row.refresh_generation or 0) + 1


def vendor_cache_usable(row: Any, *, now: datetime, cache_hours: int) -> bool:
    """A recent successful fetch is reusable, including one that reports expiry.

    Failed, stale, and unknown rows are not a cache hit: the next read must
    refresh or stay unknown rather than look current.
    """
    if row is None or getattr(row, "freshness_status", None) not in {"fresh", "expired"}:
        return False
    if getattr(row, "attempt_state", None) == "in_progress":
        return False
    return is_within(getattr(row, "fetched_at", None), now, timedelta(hours=max(cache_hours, 1)))


def evidence_status(row: Any, *, now: datetime, max_age: timedelta) -> str:
    """Recompute freshness from stored timestamps. Failed stays failed."""
    if row is None:
        return "unknown"
    status = getattr(row, "freshness_status", None) or "unknown"
    if status == "failed" or getattr(row, "attempt_state", None) in {"failed", "in_progress", "cancelled"}:
        if status == "failed" or getattr(row, "attempt_state", None) == "failed":
            return "failed"
        if getattr(row, "attempt_state", None) == "in_progress":
            return "unknown"
    fetched = getattr(row, "fetched_at", None) or getattr(row, "refreshed_at", None)
    if not is_within(fetched, now, max_age):
        return "stale" if fetched is not None else "unknown"
    expires = getattr(row, "expires_at", None) or getattr(row, "expiration_date", None)
    if isinstance(expires, datetime):
        expires = expires.date()
    if isinstance(expires, date) and expires < now.date():
        return "expired"
    if status == "fresh":
        return "fresh"
    # Rows stored before freshness existed: a recent refresh with a status is
    # current. A failed refresh clears the status, so it cannot take this path.
    if status == "unknown" and getattr(row, "registration_status", None) and is_within(fetched, now, max_age):
        return "fresh"
    return "unknown"


def positive_registration_allowed(row: Any, *, now: datetime, max_age: timedelta) -> bool:
    return evidence_status(row, now=now, max_age=max_age) == "fresh"
