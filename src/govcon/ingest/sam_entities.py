"""SAM.gov Entity Management API (Phase 6).

Verified 2026-09-26 against https://open.gsa.gov/api/entity-api/ :

- Production endpoint: ``GET https://api.sam.gov/entity-information/v3/entities``
- Authentication: required ``api_key`` query parameter (same key as opportunities)
- Lookup: ``ueiSAM`` accepts one 12-character UEI
- Sections used: ``entityRegistration``, ``coreData``, ``assertions``, ``pointsOfContact``
- Response envelope: ``entityData`` array (10 records per page; single-UEI lookup returns one)
- Public tier exposes registration status, CAGE, business types, NAICS, PSC, and POC names
- Daily quota depends on SAM.gov account role (10 / 1,000 / 10,000). HTTP 429 is retried.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session
from tenacity import wait_exponential
from tenacity.wait import wait_base

from govcon.config import Settings, get_settings
from govcon.http import build_client, request_with_retry
from govcon.ingest.freshness import (
    accept_payload,
    cancel_attempt,
    claim_attempt,
    commit_freshness,
    expiration_date,
    mark_failed,
    registration_key,
    replace_registration,
    safe_error,
    source_updated_at,
    vendor_cache_usable,
)
from govcon.models import Vendor

logger = logging.getLogger("govcon.ingest.sam_entities")

SAM_ENTITY_URL = "https://api.sam.gov/entity-information/v3/entities"
SAM_ENTITY_SECTIONS = "entityRegistration,coreData,assertions,pointsOfContact"
SAM_ENTITY_RETRY_ATTEMPTS = 5
SAM_ENTITY_RETRY_WAIT = wait_exponential(multiplier=1, min=1, max=32)


class SamEntityError(RuntimeError):
    """The SAM.gov entity lookup could not be completed."""


@dataclass(frozen=True)
class ParsedEntity:
    uei: str
    cage_code: str | None
    legal_name: str | None
    dba_name: str | None
    registration_status: str | None
    physical_address: dict | None
    business_types: dict | None
    naics_codes: list | None
    psc_codes: list | None
    points_of_contact: dict | list | None
    raw: dict


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    text = str(value).strip()
    return text or None


def normalize_uei(value: str) -> str:
    text = (value or "").strip().upper()
    if len(text) != 12:
        raise ValueError(f"UEI must be 12 characters, got {len(text)}")
    return text


def _opt_out(value: object) -> bool:
    text = _text(value)
    if not text:
        return False
    return "opted out of public search" in text.casefold()


def _list_of_codes(items: object, code_key: str, desc_key: str) -> list | None:
    if not isinstance(items, list):
        return None
    codes: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        code = _text(item.get(code_key))
        if code is None:
            continue
        entry = {"code": code}
        description = _text(item.get(desc_key))
        if description:
            entry["description"] = description
        codes.append(entry)
    return codes or None


def parse_entity_record(record: dict) -> ParsedEntity | None:
    """Map one ``entityData`` object to vendor columns."""
    if not isinstance(record, dict):
        return None
    registration = record.get("entityRegistration")
    if not isinstance(registration, dict):
        return None
    uei = _text(registration.get("ueiSAM"))
    if uei is None or _opt_out(registration.get("ueiSAM")):
        return None
    if _opt_out(registration.get("legalBusinessName")):
        return None

    core = record.get("coreData") if isinstance(record.get("coreData"), dict) else {}
    assertions = record.get("assertions") if isinstance(record.get("assertions"), dict) else {}
    business_types = core.get("businessTypes") if isinstance(core.get("businessTypes"), dict) else None

    naics_codes = _list_of_codes(assertions.get("naicsList"), "naicsCode", "naicsDescription")
    if naics_codes is None:
        naics_codes = _list_of_codes(assertions.get("naicsList"), "naicsCode", "naicsName")
    psc_codes = _list_of_codes(assertions.get("pscList"), "pscCode", "pscDescription")

    physical = core.get("physicalAddress")
    physical_address = physical if isinstance(physical, dict) else None
    points = record.get("pointsOfContact")
    if points is not None and not isinstance(points, (dict, list)):
        points = None

    registration_status = _text(registration.get("registrationStatus"))
    if registration_status and _opt_out(registration_status):
        registration_status = None

    return ParsedEntity(
        uei=uei.upper(),
        cage_code=_text(registration.get("cageCode")),
        legal_name=_text(registration.get("legalBusinessName")),
        dba_name=_text(registration.get("dbaName")),
        registration_status=registration_status,
        physical_address=physical_address,
        business_types=business_types,
        naics_codes=naics_codes,
        psc_codes=psc_codes,
        points_of_contact=points,
        raw=record,
    )


def fetch_entity_payload(
    client: httpx.Client,
    settings: Settings,
    uei: str,
    *,
    wait: wait_base | None = None,
) -> dict:
    """Call the SAM entity API for one UEI and return the first ``entityData`` record."""
    normalized = normalize_uei(uei)
    params = {
        "api_key": settings.require_sam_api_key(),
        "ueiSAM": normalized,
        "includeSections": SAM_ENTITY_SECTIONS,
    }
    response = request_with_retry(
        client,
        "GET",
        SAM_ENTITY_URL,
        params=params,
        attempts=SAM_ENTITY_RETRY_ATTEMPTS,
        wait=wait or SAM_ENTITY_RETRY_WAIT,
    )
    if response.status_code == 404:
        raise SamEntityError(f"SAM entity not found for UEI {normalized}")
    if response.status_code >= 400:
        raise SamEntityError(
            f"SAM entity lookup failed with HTTP {response.status_code}: "
            f"{response.text[:240]}"
        )
    payload = response.json()
    rows = payload.get("entityData")
    if not isinstance(rows, list) or not rows:
        raise SamEntityError(f"SAM entity response empty for UEI {normalized}")
    first = rows[0]
    if not isinstance(first, dict):
        raise SamEntityError(f"SAM entity response malformed for UEI {normalized}")
    return first


def _load_vendor(session: Session, uei: str) -> Vendor | None:
    row = session.get(Vendor, uei)
    if row is not None:
        session.refresh(row)
    return row


def ensure_vendor(
    session: Session,
    uei: str,
    *,
    refresh: bool = False,
    client: httpx.Client | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> tuple[Vendor, bool]:
    """Return a vendor row, fetching from SAM when the cache is not fresh.

    The second value is ``True`` when a newer payload was stored. A failed
    refresh clears that registration's assertions and raises
    :class:`SamEntityError`. A response older than the stored source time,
    or one whose attempt was cancelled or restarted, is not applied.
    """
    settings = settings or get_settings()
    normalized = normalize_uei(uei)
    now = now or datetime.now(timezone.utc)
    existing = _load_vendor(session, normalized)
    if existing is not None and not refresh and vendor_cache_usable(
        existing, now=now, cache_hours=settings.sam_vendor_cache_hours
    ):
        return existing, False

    row = existing or Vendor(uei=normalized)
    if existing is None:
        session.add(row)
    attempt_id = claim_attempt(row, now)
    session.flush()

    owns_client = client is None
    client = client or build_client(settings)
    try:
        try:
            record = fetch_entity_payload(client, settings, normalized)
        except Exception as exc:
            current = _load_vendor(session, normalized) or row
            mark_failed(current, attempt_id, safe_error(exc, settings), now, kind="vendor")
            session.flush()
            session.expire(current)
            raise SamEntityError(safe_error(exc, settings)) from exc
    finally:
        if owns_client:
            client.close()

    return _store_vendor_record(session, normalized, attempt_id, record, now)


def _store_vendor_record(
    session: Session,
    uei: str,
    attempt_id: str,
    record: dict,
    now: datetime,
) -> tuple[Vendor, bool]:
    """Publish ``record`` for ``attempt_id``, or leave the row unchanged."""
    parsed = parse_entity_record(record)
    current = _load_vendor(session, uei)
    if current is None:
        raise SamEntityError(f"SAM vendor row disappeared before publish for UEI {uei}")
    if parsed is None:
        mark_failed(current, attempt_id, f"SAM entity payload could not be parsed for UEI {uei}", now, kind="vendor")
        session.flush()
        session.expire(current)
        raise SamEntityError(f"SAM entity payload could not be parsed for UEI {uei}")
    updated = source_updated_at(parsed.raw)
    if not accept_payload(current, attempt_id, now=now, source_updated=updated):
        session.flush()
        session.expire(current)
        reloaded = _load_vendor(session, uei)
        return (reloaded or current), False
    replace_registration(current, key=registration_key(uei, parsed.raw), kind="vendor")
    # Every SAM field is assigned, including nulls, so a missing value cannot
    # keep the previous registration's assertion.
    current.cage_code = parsed.cage_code
    current.legal_name = parsed.legal_name
    current.dba_name = parsed.dba_name
    current.registration_status = parsed.registration_status
    current.physical_address = parsed.physical_address
    current.business_types = parsed.business_types
    current.naics_codes = parsed.naics_codes
    current.psc_codes = parsed.psc_codes
    current.points_of_contact = parsed.points_of_contact
    current.raw = parsed.raw
    commit_freshness(current, now=now, source_updated=updated, expires=expiration_date(parsed.raw), kind="vendor")
    session.flush()
    session.expire(current)
    stored = _load_vendor(session, uei)
    return (stored or current), True


def cancel_vendor_refresh(session: Session, uei: str, attempt_id: str) -> bool:
    """Cancel an in-flight vendor refresh so a late response cannot apply."""
    row = _load_vendor(session, normalize_uei(uei))
    if row is None:
        return False
    cancelled = cancel_attempt(row, attempt_id)
    if cancelled:
        session.flush()
        session.expire(row)
    return cancelled
