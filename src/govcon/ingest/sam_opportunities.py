"""SAM.gov Get Opportunities Public API ingestion.

Verified 2026-09-26 against https://open.gsa.gov/api/get-opportunities-public-api/ :

- Production endpoint: ``https://api.sam.gov/opportunities/v2/search``
- Authentication: required ``api_key`` query parameter
- ``postedFrom`` and ``postedTo`` are mandatory, ``MM/dd/yyyy``, and at most one year apart
- ``limit`` is records per page, maximum 1000 (the API default is 1)
- ``offset`` is the page index, starting at 0
- Response envelope: ``totalRecords``, ``limit``, ``offset``, ``opportunitiesData``
- Notice id is ``noticeId`` (query parameter ``noticeid``). The public API returns
  only the latest version and does not publish a separate amendment id.
- Attachments are ``resourceLinks``. ``description`` is a URL, not the body.
- Daily quotas depend on the SAM.gov account role. HTTP 429 is retried with
  exponential backoff and then fails the run.

The default search uses the documented maximum page size so a small daily quota
is not spent retrieving one record at a time. Description bodies are separate
requests and are not downloaded by this job.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from tenacity import wait_exponential
from tenacity.wait import wait_base

from govcon.config import Settings, get_settings
from govcon.http import RETRYABLE_STATUS, build_client, request_with_retry
from govcon.ingest.runs import IngestStats
from govcon.ingest.status import opportunity_status
from govcon.ingest.snapshots import (
    SOURCE_SAM,
    NormalizedOpportunity,
    canonical_content_hash,
    description_hash,
    upsert_opportunity,
)
from govcon.logging import redact
from govcon.models import Opportunity, OpportunityEvent

logger = logging.getLogger("govcon.ingest.sam")

SAM_SEARCH_URL = "https://api.sam.gov/opportunities/v2/search"
SAM_PAGE_LIMIT = 1000
SAM_RETRY_ATTEMPTS = 5
SAM_RETRY_WAIT = wait_exponential(multiplier=1, min=1, max=32)

_NSN_LABELED = re.compile(
    r"\b(?:NSN|National\s+Stock\s+Number)\b[:\s#-]*"
    r"(\d{4})\s*[- ]\s*(\d{2})\s*[- ]\s*(\d{3})\s*[- ]\s*(\d{4})\b",
    re.IGNORECASE,
)
_NSN_DASHED = re.compile(r"\b(\d{4})-(\d{2})-(\d{3})-(\d{4})\b")
_QUANTITY = re.compile(
    r"\b(?:qty|quantity)\b\s*[:#-]?\s*(\d{1,7}(?:,\d{3})*(?:\.\d+)?)"
    r"(?:\s+(ea|each|unit|units|lot|lots|dz|dozen|pr|pair|set|sets|pkg|box|boxes)\b)?",
    re.IGNORECASE,
)
_ESTIMATED_RANGE = re.compile(
    r"\bestimated\s+(?:contract\s+)?(?:value|cost|amount|price)\b"
    r"[^$\n]{0,80}\$\s*([\d,]+(?:\.\d+)?)\s*(?:to|through|-|–|—)\s*\$\s*([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
_ESTIMATED_SINGLE = re.compile(
    r"\bestimated\s+(?:contract\s+)?(?:value|cost|amount|price)\b"
    r"[^$\n]{0,80}\$\s*([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
_EXPLICIT_VALUE_KEYS = ("estimatedValue", "estimated_value", "estimatedAmount", "estimated_amount")


class SamApiError(RuntimeError):
    """The SAM.gov opportunities search could not be completed."""


def format_sam_date(value: date) -> str:
    return value.strftime("%m/%d/%Y")


def parse_user_date(value: str) -> date:
    text = value.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid date {value!r}. Use MM/dd/yyyy or YYYY-MM-DD.")


def _add_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(year=value.year + years, day=28)


def assert_search_window(posted_from: date, posted_to: date) -> None:
    """Reject ranges the Get Opportunities API documents as invalid."""
    if posted_to < posted_from:
        raise ValueError("postedTo is before postedFrom")
    if posted_to > _add_years(posted_from, 1):
        raise ValueError("postedFrom and postedTo must be at most 1 year apart; use sam-backfill")


def default_posted_window(today: date | None = None) -> tuple[date, date]:
    """Inclusive last three UTC calendar days, including today."""
    today = today or datetime.now(timezone.utc).date()
    return today - timedelta(days=2), today


def iter_backfill_windows(posted_from: date, posted_to: date) -> list[tuple[date, date]]:
    """Split a posted-date range into windows of at most one calendar year."""
    if posted_to < posted_from:
        raise ValueError("postedTo is before postedFrom")
    windows: list[tuple[date, date]] = []
    cursor = posted_from
    while cursor <= posted_to:
        window_end = min(posted_to, _add_years(cursor, 1))
        windows.append((cursor, window_end))
        if window_end >= posted_to:
            break
        cursor = window_end + timedelta(days=1)
    return windows


def _text(value: object) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    if text == "" or text.lower() == "null":
        return None
    return text


def _parse_date(value: object) -> date | None:
    text = _text(value)
    if text is None:
        return None
    for candidate, fmt in ((text[:10], "%Y-%m-%d"), (text[:10], "%m/%d/%Y")):
        try:
            return datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    return None


def _parse_datetime(value: object) -> datetime | None:
    text = _text(value)
    if text is None:
        return None
    try:
        parsed: datetime | None = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    text = _text(value)
    if text is None:
        return None
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def _search_text(title: str | None, description: str | None) -> str:
    return "\n".join(part for part in (title, description) if part)


def parse_nsn_candidates(title: str | None, description: str | None) -> tuple[str, ...]:
    """Return full 13-digit NSNs written with dashes or an NSN label."""
    text = _search_text(title, description)
    found: list[str] = []
    for pattern in (_NSN_LABELED, _NSN_DASHED):
        for match in pattern.finditer(text):
            nsn = f"{match.group(1)}-{match.group(2)}-{match.group(3)}-{match.group(4)}"
            if nsn not in found:
                found.append(nsn)
    return tuple(found)


def parse_quantity(title: str | None, description: str | None) -> tuple[Decimal | None, str | None]:
    """Parse quantity only from an explicit qty/quantity label."""
    match = _QUANTITY.search(_search_text(title, description))
    if match is None:
        return None, None
    return _decimal(match.group(1)), _text(match.group(2))


def _value_from_explicit_fields(raw: dict) -> tuple[Decimal | None, Decimal | None, str | None]:
    for key in _EXPLICIT_VALUE_KEYS:
        if key not in raw:
            continue
        value = raw.get(key)
        if isinstance(value, dict):
            low = _decimal(value.get("min"))
            high = _decimal(value.get("max"))
            if low is None and high is None:
                continue
            return low if low is not None else high, high if high is not None else low, key
        amount = _decimal(value)
        if amount is not None:
            return amount, amount, key
    return None, None, None


def parse_estimated_values(
    raw: dict, title: str | None, description: str | None
) -> tuple[Decimal | None, Decimal | None, str | None]:
    """Use an explicit estimated-value field or labeled estimate text.

    Award amounts are obligations, not estimates, and are ignored.
    """
    low, high, source = _value_from_explicit_fields(raw)
    if source is not None:
        return low, high, source
    text = _search_text(title, description)
    ranged = _ESTIMATED_RANGE.search(text)
    if ranged is not None:
        return _decimal(ranged.group(1)), _decimal(ranged.group(2)), "explicit_text"
    single = _ESTIMATED_SINGLE.search(text)
    if single is not None:
        amount = _decimal(single.group(1))
        return amount, amount, "explicit_text"
    return None, None, None


def _description_body(value: object) -> str | None:
    text = _text(value)
    if text is None:
        return None
    if text.startswith(("http://", "https://")):
        return None
    return text


def _status(raw: dict) -> str:
    kinds = " ".join(filter(None, (_text(raw.get(key)) for key in ("type", "baseType", "archiveType"))))
    if "cancel" in kinds.lower():
        return "cancelled"
    return opportunity_status(
        source=SOURCE_SAM,
        notice_type=_text(raw.get("type") or raw.get("baseType") or raw.get("archiveType")),
        active=_text(raw.get("active")),
        response_deadline=_parse_datetime(
            raw.get("responseDeadLine") or raw.get("reponseDeadLine") or raw.get("responseDeadline")
        ),
        archive_date=_parse_date(raw.get("archiveDate")),
    )


def _agency_path(raw: dict) -> str | None:
    path = _text(raw.get("fullParentPathName"))
    if path:
        return path
    parts = [_text(raw.get(key)) for key in ("department", "subTier", "office")]
    joined = ".".join(part for part in parts if part)
    return joined or None


def _links(raw: dict) -> dict:
    attachments: list[str] = []
    resource_links = raw.get("resourceLinks")
    if isinstance(resource_links, list):
        for item in resource_links:
            text = _text(item)
            if text and text.startswith(("http://", "https://")):
                attachments.append(text)
    elif isinstance(resource_links, str):
        text = _text(resource_links)
        if text and text.startswith(("http://", "https://")):
            attachments.append(text)

    self_links: list[dict] = []
    links = raw.get("links")
    if isinstance(links, list):
        for item in links:
            if not isinstance(item, dict):
                continue
            href = _text(item.get("href"))
            if href is None:
                continue
            self_links.append({"rel": _text(item.get("rel")), "href": href})

    description = _text(raw.get("description"))
    description_url = description if description and description.startswith(("http://", "https://")) else None
    payload = {
        "ui": _text(raw.get("uiLink")),
        "additional_info": _text(raw.get("additionalInfoLink")),
        "description": description_url,
        "self": self_links,
        "attachments": attachments,
    }
    return {key: value for key, value in payload.items() if value not in (None, [], "")}


def _contacts(raw: dict) -> tuple[dict, ...]:
    points = raw.get("pointOfContact")
    if points is None:
        points = raw.get("pointofContact")
    if isinstance(points, dict):
        points = [points]
    if not isinstance(points, list):
        return ()
    contacts: list[dict] = []
    for point in points:
        if not isinstance(point, dict):
            continue
        email = _text(point.get("email"))
        name = _text(point.get("fullName") or point.get("fullname") or point.get("name"))
        contacts.append(
            {
                "name": name,
                "email": email.lower() if email else None,
                "phone": _text(point.get("phone")),
                "title": _text(point.get("title")),
                "contact_type": _text(point.get("type")),
            }
        )
    return tuple(contacts)


def _point_of_contact_payload(raw: dict) -> list | None:
    points = raw.get("pointOfContact")
    if points is None:
        points = raw.get("pointofContact")
    if isinstance(points, list):
        return points
    if isinstance(points, dict):
        return [points]
    return None


def normalize_opportunity(raw: dict) -> NormalizedOpportunity:
    if not isinstance(raw, dict):
        raise ValueError("SAM opportunity payload must be an object")
    notice_id = _text(raw.get("noticeId"))
    if notice_id is None:
        raise ValueError("SAM opportunity is missing noticeId")
    title = _text(raw.get("title"))
    description = _description_body(raw.get("description"))
    nsn_candidates = parse_nsn_candidates(title, description)
    quantity, unit = parse_quantity(title, description)
    value_min, value_max, value_source = parse_estimated_values(raw, title, description)
    posted = _text(raw.get("postedDate"))
    return NormalizedOpportunity(
        source=SOURCE_SAM,
        source_id=notice_id,
        solicitation_number=_text(raw.get("solicitationNumber")),
        title=title,
        description=description,
        description_hash=description_hash(description),
        opportunity_type=_text(raw.get("type")),
        psc_code=_text(raw.get("classificationCode")),
        naics_code=_text(raw.get("naicsCode")),
        set_aside_code=_text(raw.get("typeOfSetAside") or raw.get("setAsideCode")),
        agency_path=_agency_path(raw),
        place_of_performance=raw.get("placeOfPerformance") if isinstance(raw.get("placeOfPerformance"), dict) else None,
        nsn=nsn_candidates[0] if nsn_candidates else None,
        nsn_candidates=nsn_candidates,
        quantity=quantity,
        unit=unit.lower() if unit else None,
        estimated_value_min=value_min,
        estimated_value_max=value_max,
        estimated_value_source=value_source,
        posted_date=_parse_date(raw.get("postedDate")),
        response_deadline=_parse_datetime(raw.get("responseDeadLine") or raw.get("reponseDeadLine") or raw.get("responseDeadline")),
        archive_date=_parse_date(raw.get("archiveDate")),
        status=_status(raw),
        poc=_point_of_contact_payload(raw),
        links=_links(raw),
        raw=raw,
        content_hash=canonical_content_hash(raw),
        source_version=posted or notice_id,
        contacts=_contacts(raw),
    )


def _json_object(response: httpx.Response) -> dict | None:
    content_type = response.headers.get("content-type", "")
    if "json" not in content_type.lower() and not response.content.startswith((b"{", b"[")):
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def decode_search_response(response: httpx.Response) -> dict:
    if response.status_code in RETRYABLE_STATUS:
        raise SamApiError(f"SAM opportunities search failed with HTTP {response.status_code}")
    if response.status_code == 404:
        payload = _json_object(response)
        if payload is not None and ("opportunitiesData" in payload or "totalRecords" in payload):
            payload.setdefault("opportunitiesData", [])
            return payload
        raise SamApiError("SAM opportunities search returned HTTP 404 without a search payload")
    if response.status_code != 200:
        detail = redact(response.text[:300]) if response.text else ""
        message = f"SAM opportunities search failed with HTTP {response.status_code}"
        if detail:
            message = f"{message}: {detail}"
        raise SamApiError(message)
    payload = _json_object(response)
    if payload is None:
        raise SamApiError("SAM opportunities search returned a non-object payload")
    return payload


def fetch_search_page(
    client: httpx.Client,
    *,
    api_key: str,
    posted_from: date,
    posted_to: date,
    offset: int,
    limit: int,
    attempts: int = SAM_RETRY_ATTEMPTS,
    wait: wait_base | None = None,
) -> dict:
    assert_search_window(posted_from, posted_to)
    if limit < 1 or limit > SAM_PAGE_LIMIT:
        raise ValueError("SAM limit must be from 1 to 1000")
    logger.info(
        "SAM search postedFrom=%s postedTo=%s offset=%s limit=%s",
        format_sam_date(posted_from),
        format_sam_date(posted_to),
        offset,
        limit,
    )
    response = request_with_retry(
        client,
        "GET",
        SAM_SEARCH_URL,
        params={
            "api_key": api_key,
            "postedFrom": format_sam_date(posted_from),
            "postedTo": format_sam_date(posted_to),
            "limit": limit,
            "offset": offset,
        },
        attempts=attempts,
        wait=wait if wait is not None else SAM_RETRY_WAIT,
    )
    return decode_search_response(response)


def iter_search_records(
    client: httpx.Client,
    *,
    api_key: str,
    posted_from: date,
    posted_to: date,
    limit: int = SAM_PAGE_LIMIT,
    attempts: int = SAM_RETRY_ATTEMPTS,
    wait: wait_base | None = None,
) -> Iterator[dict]:
    """Yield every opportunity, following ``offset`` as a page index."""
    offset = 0
    fetched = 0
    previous_ids: tuple[str, ...] | None = None
    seen_ids: set[str] = set()
    while True:
        page = fetch_search_page(
            client,
            api_key=api_key,
            posted_from=posted_from,
            posted_to=posted_to,
            offset=offset,
            limit=limit,
            attempts=attempts,
            wait=wait,
        )
        rows = page.get("opportunitiesData") or []
        if not isinstance(rows, list):
            raise SamApiError("SAM search payload missing opportunitiesData")
        total = int(page.get("totalRecords") or 0)
        page_ids = tuple(str(row.get("noticeId") or "") for row in rows if isinstance(row, dict))
        if previous_ids and page_ids:
            overlap = len(set(previous_ids) & set(page_ids)) / len(page_ids)
            if overlap >= 0.5:
                raise SamApiError(
                    "SAM search page repeated notice ids from the previous page. "
                    "offset is documented as a page index; the run stopped instead of truncating."
                )
        previous_ids = page_ids
        for row in rows:
            if not isinstance(row, dict):
                continue
            notice_id = str(row.get("noticeId") or "")
            if notice_id and notice_id in seen_ids:
                continue
            if notice_id:
                seen_ids.add(notice_id)
            yield row
        fetched += len(rows)
        if not rows or fetched >= total:
            return
        offset += 1
        if offset > 10000:
            raise SamApiError("SAM search exceeded the page safety limit")


def ingest_opportunity_records(session: Session, records: list[dict]) -> IngestStats:
    stats = IngestStats(fetched=len(records))
    for record in records:
        try:
            with session.begin_nested():
                outcome = upsert_opportunity(session, normalize_opportunity(record))
        except Exception as exc:
            stats.errors.append(redact(str(exc)))
            logger.warning("SAM record skipped: %s", redact(str(exc)))
            continue
        if outcome == "inserted":
            stats.inserted += 1
        elif outcome == "updated":
            stats.updated += 1
        else:
            stats.unchanged += 1
    return stats


def pull_sam_opportunities(
    session: Session,
    *,
    api_key: str,
    posted_from: date,
    posted_to: date,
    limit: int = SAM_PAGE_LIMIT,
    client: httpx.Client | None = None,
    settings: Settings | None = None,
    attempts: int = SAM_RETRY_ATTEMPTS,
    wait: wait_base | None = None,
) -> IngestStats:
    own_client = client is None
    client = client or build_client(settings or get_settings())
    try:
        records = list(
            iter_search_records(
                client,
                api_key=api_key,
                posted_from=posted_from,
                posted_to=posted_to,
                limit=limit,
                attempts=attempts,
                wait=wait,
            )
        )
        return ingest_opportunity_records(session, records)
    finally:
        if own_client:
            client.close()


def backfill_sam_opportunities(
    session: Session,
    *,
    api_key: str,
    posted_from: date,
    posted_to: date,
    limit: int = SAM_PAGE_LIMIT,
    client: httpx.Client | None = None,
    settings: Settings | None = None,
    attempts: int = SAM_RETRY_ATTEMPTS,
    wait: wait_base | None = None,
) -> IngestStats:
    """Pull each API-safe posted-date window in order."""
    totals = IngestStats()
    for window_from, window_to in iter_backfill_windows(posted_from, posted_to):
        stats = pull_sam_opportunities(
            session,
            api_key=api_key,
            posted_from=window_from,
            posted_to=window_to,
            limit=limit,
            client=client,
            settings=settings,
            attempts=attempts,
            wait=wait,
        )
        totals.add(stats)
    return totals


def archive_expired_sam_opportunities(session: Session, *, today: date | None = None) -> IngestStats:
    """Mark locally stored SAM rows archived when ``archive_date`` is past.

    This task does not call SAM.gov. It does not write a new snapshot because
    no new source payload was fetched.
    """
    today = today or datetime.now(timezone.utc).date()
    rows = session.scalars(
        select(Opportunity).where(
            Opportunity.source == SOURCE_SAM,
            Opportunity.status == "open",
            Opportunity.archive_date.is_not(None),
            Opportunity.archive_date < today,
        )
    ).all()
    stats = IngestStats(fetched=len(rows))
    for row in rows:
        previous = row.status
        row.status = "archived"
        session.add(
            OpportunityEvent(
                opportunity_id=row.id,
                event_type="status_changed",
                field_name="status",
                old_value={"value": previous},
                new_value={"value": "archived"},
                snapshot_id=None,
            )
        )
        stats.updated += 1
    return stats
