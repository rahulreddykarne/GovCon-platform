"""USAspending contract award ingestion.

Verified 2026-09-26 against the live API and the published contracts:

- ``POST https://api.usaspending.gov/api/v2/search/spending_by_award/``
- No API key. Contract award types are ``A``, ``B``, ``C``, and ``D``.
- ``limit`` defaults to 10 and rejects values above 100.
- ``page`` is 1-based. ``page_metadata.hasNext`` continues the search.
- PSC filters accept a code or a shorter prefix (``R4`` returned ``R402``).
- NAICS filters use ``{"require": [...]}`` and match prefixes.
- ``time_period.date_type`` applies to both bounds when set. This client sets
  ``action_date`` for the initial lookback and ``last_modified_date`` for
  later pulls. Omitting ``date_type`` would compare the start to action date
  and the end to date signed.
- Search rows expose ``generated_internal_id``, ``Award ID``, ``Recipient Name``,
  ``Recipient UEI``, ``Award Amount``, ``Base Obligation Date``, ``Start Date``,
  ``End Date``, ``Description``, ``PSC.code``, and ``NAICS.code``.
- Neither the search fields nor ``GET /api/v2/awards/{id}/`` publish quantity
  or unit price. Those stay null unless the payload or the description states
  them. Award amount is never divided into a unit price.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from tenacity.wait import wait_base

from govcon.config import Settings, get_settings
from govcon.http import build_client, request_with_retry
from govcon.ingest.runs import IngestStats
from govcon.ingest.sam_opportunities import parse_nsn_candidates, parse_quantity
from govcon.ingest.snapshots import canonical_content_hash
from govcon.logging import redact
from govcon.models import Award, IngestionRun, Watchlist

logger = logging.getLogger("govcon.ingest.usaspending")

SEARCH_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
SOURCE = "usaspending"
JOB_NAME = "usaspending_awards"
CONTRACT_AWARD_TYPES = ("A", "B", "C", "D")
PAGE_LIMIT = 100
MAX_PAGES = 1000
LOOKBACK_YEARS = 3
COMPLETED_STATUSES = ("succeeded", "completed_with_errors")
WATERMARK_MODES = ("backfill", "incremental", "explicit")
SEARCH_FIELDS = (
    "Award ID",
    "Recipient Name",
    "Recipient UEI",
    "Start Date",
    "End Date",
    "Award Amount",
    "Total Outlays",
    "Description",
    "Awarding Agency",
    "Awarding Sub Agency",
    "Contract Award Type",
    "NAICS",
    "PSC",
    "generated_internal_id",
    "Last Modified Date",
    "Base Obligation Date",
)

_UNIT_PRICE = re.compile(
    r"\bunit\s+(?:price|cost)\b\s*[:#-]?\s*\$?\s*(\d{1,12}(?:,\d{3})*(?:\.\d+)?)",
    re.IGNORECASE,
)
_EXPLICIT_QUANTITY_KEYS = ("quantity", "Quantity")
_EXPLICIT_PRICE_KEYS = ("unit_price", "unitPrice", "Unit Price")
_SET_ASIDE_KEYS = ("type_set_aside", "set_aside_code", "Set Aside")


class UsaSpendingError(RuntimeError):
    """The USAspending award search could not be completed."""

    def __init__(self, message: str, stats: IngestStats | None = None) -> None:
        super().__init__(message)
        self.stats = stats or IngestStats()


@dataclass(frozen=True)
class PullPlan:
    mode: str
    date_type: str
    start: date
    end: date
    # Codes from enabled watchlists when the plan was made (recorded on the run).
    psc_codes: tuple[str, ...] = ()
    naics_codes: tuple[str, ...] = ()
    # Incremental pulls only: codes no earlier pull covered. They get the
    # 3-year action-date lookback in addition to the incremental window.
    backfill_psc_codes: tuple[str, ...] = ()
    backfill_naics_codes: tuple[str, ...] = ()
    backfill_start: date | None = None
    backfill_end: date | None = None


@dataclass(frozen=True)
class NormalizedAward:
    award_id: str
    piid: str | None
    description: str | None
    psc_code: str | None
    naics_code: str | None
    nsn: str | None
    recipient_uei: str | None
    recipient_name: str | None
    awarding_agency: str | None
    action_date: date | None
    total_obligation: Decimal | None
    quantity: Decimal | None
    unit_price: Decimal | None
    set_aside_code: str | None


def parse_user_date(value: str) -> date:
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid date {value!r}. Use YYYY-MM-DD or MM/dd/yyyy.")


def _add_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(year=value.year + years, day=28)


def default_lookback(today: date | None = None) -> tuple[date, date]:
    """Inclusive action-date window covering the last three years through today."""
    today = today or datetime.now(UTC).date()
    return _add_years(today, -LOOKBACK_YEARS), today


def _text(value: object) -> str | None:
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = str(value).strip()
    if text == "" or text.lower() == "null":
        return None
    return text


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    text = _text(value)
    if text is None:
        return None
    try:
        return Decimal(text.replace(",", "").replace("$", ""))
    except InvalidOperation:
        return None


def _parse_date(value: object) -> date | None:
    text = _text(value)
    if text is None:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text[:10], fmt).replace(tzinfo=UTC).date()
        except ValueError:
            continue
    return None


def _code(value: object) -> str | None:
    if isinstance(value, dict):
        return _text(value.get("code"))
    return _text(value)


def _num_text(value: float | Decimal) -> str:
    if isinstance(value, float):
        number = Decimal(str(value))
    elif isinstance(value, Decimal):
        number = value
    else:
        number = Decimal(value)
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _canonicalize(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _canonicalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonicalize(item) for item in value]
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return _num_text(value)
    return str(value)


def _stable_hash(payload: dict) -> str:
    canonical = _canonicalize(payload)
    if not isinstance(canonical, dict):
        raise UsaSpendingError("award payload did not canonicalize to an object")
    return canonical_content_hash(canonical)


def _first_explicit_decimal(payload: dict, keys: tuple[str, ...]) -> Decimal | None:
    for key in keys:
        if key not in payload:
            continue
        parsed = _decimal(payload.get(key))
        if parsed is not None:
            return parsed
    return None


def _unit_price_from_text(description: str | None) -> Decimal | None:
    if not description:
        return None
    match = _UNIT_PRICE.search(description)
    if match is None:
        return None
    return _decimal(match.group(1))


def derive_quantity_and_unit_price(
    payload: dict, description: str | None
) -> tuple[Decimal | None, Decimal | None]:
    """Use an explicit quantity or unit price. Never divide the obligation."""
    quantity = _first_explicit_decimal(payload, _EXPLICIT_QUANTITY_KEYS)
    if quantity is None:
        quantity, _unit = parse_quantity(None, description)
    unit_price = _first_explicit_decimal(payload, _EXPLICIT_PRICE_KEYS)
    if unit_price is None:
        unit_price = _unit_price_from_text(description)
    return quantity, unit_price


def _set_aside(payload: dict) -> str | None:
    for key in _SET_ASIDE_KEYS:
        if key in payload:
            return _text(payload.get(key))
    contract = payload.get("latest_transaction_contract_data")
    if isinstance(contract, dict) and "type_set_aside" in contract:
        return _text(contract.get("type_set_aside"))
    return None


def _agency(payload: dict) -> str | None:
    top = _text(payload.get("Awarding Agency"))
    sub = _text(payload.get("Awarding Sub Agency"))
    if top and sub and sub.casefold() != top.casefold():
        return f"{top} / {sub}"
    return top or sub


def normalize_award(payload: dict) -> NormalizedAward | None:
    """Map one search result. Returns None when the stable award id is missing."""
    award_id = _text(payload.get("generated_internal_id"))
    if award_id is None:
        return None
    description = _text(payload.get("Description"))
    nsns = parse_nsn_candidates(None, description)
    quantity, unit_price = derive_quantity_and_unit_price(payload, description)
    return NormalizedAward(
        award_id=award_id,
        piid=_text(payload.get("Award ID")),
        description=description,
        psc_code=_code(payload.get("PSC")),
        naics_code=_code(payload.get("NAICS")),
        nsn=nsns[0] if nsns else None,
        recipient_uei=_text(payload.get("Recipient UEI")),
        recipient_name=_text(payload.get("Recipient Name")),
        awarding_agency=_agency(payload),
        action_date=_parse_date(payload.get("Base Obligation Date")) or _parse_date(payload.get("Start Date")),
        total_obligation=_decimal(payload.get("Award Amount")),
        quantity=quantity,
        unit_price=unit_price,
        set_aside_code=_set_aside(payload),
    )


def _apply(row: Award, item: NormalizedAward, payload: dict) -> None:
    row.piid = item.piid
    row.description = item.description
    row.psc_code = item.psc_code
    row.naics_code = item.naics_code
    row.nsn = item.nsn
    row.recipient_uei = item.recipient_uei
    row.recipient_name = item.recipient_name
    row.awarding_agency = item.awarding_agency
    row.action_date = item.action_date
    row.total_obligation = item.total_obligation
    row.quantity = item.quantity
    row.unit_price = item.unit_price
    row.set_aside_code = item.set_aside_code
    row.raw = payload


def upsert_award(session: Session, payload: dict) -> str:
    """Insert or update one award. An unchanged raw payload does not write."""
    item = normalize_award(payload)
    if item is None:
        raise ValueError("award missing generated_internal_id")
    existing = session.scalar(
        select(Award).where(Award.source == SOURCE, Award.award_id == item.award_id)
    )
    incoming = _stable_hash(payload)
    if existing is not None:
        if _stable_hash(existing.raw if isinstance(existing.raw, dict) else {}) == incoming:
            return "unchanged"
        _apply(existing, item, payload)
        return "updated"
    row = Award(source=SOURCE, award_id=item.award_id, raw=payload)
    _apply(row, item, payload)
    session.add(row)
    return "inserted"


def load_search_document(path: Path) -> list[dict]:
    """Read a spending_by_award response or a bare list of award objects."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsaSpendingError(f"could not read award file: {exc}") from exc
    if isinstance(document, dict):
        rows = document.get("results")
    elif isinstance(document, list):
        rows = document
    else:
        rows = None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise UsaSpendingError("award file must be a search response or a list of award objects")
    return rows


def _unique_codes(values: list[str] | None) -> list[str]:
    found: list[str] = []
    for value in values or []:
        text = value.strip()
        if text and text not in found:
            found.append(text)
    return found


def collect_watchlist_codes(session: Session) -> tuple[list[str], list[str]]:
    """PSC and NAICS codes from enabled watchlists. Disabled rows are ignored."""
    rows = session.scalars(select(Watchlist).where(Watchlist.enabled.is_(True)).order_by(Watchlist.id))
    psc_codes: list[str] = []
    naics_codes: list[str] = []
    for row in rows:
        for code in _unique_codes(row.psc_codes):
            if code not in psc_codes:
                psc_codes.append(code)
        for code in _unique_codes(row.naics_codes):
            if code not in naics_codes:
                naics_codes.append(code)
    return psc_codes, naics_codes


def last_completed_window_end(session: Session) -> date | None:
    """End date of the latest successful network pull. File ingests do not count."""
    run = session.scalar(
        select(IngestionRun)
        .where(
            IngestionRun.job == JOB_NAME,
            IngestionRun.status.in_(COMPLETED_STATUSES),
            IngestionRun.errors["details"]["mode"].astext.in_(WATERMARK_MODES),
        )
        .order_by(IngestionRun.id.desc())
        .limit(1)
    )
    if run is None or not isinstance(run.errors, dict):
        return None
    details = run.errors.get("details")
    if not isinstance(details, dict):
        return None
    return _parse_date(details.get("window_end"))


# Run modes whose recorded codes have their full lookback history.
HISTORY_MODES = ("backfill", "incremental")


def covered_codes(session: Session, current: tuple[list[str], list[str]]) -> tuple[set[str], set[str]]:
    """PSC and NAICS codes whose history earlier pulls already fetched.

    Each completed backfill or incremental run records every code it pulled,
    after backfilling any new ones, so the latest such run's codes are the
    covered set. A run from before codes were recorded counts as covering the
    codes enabled now, so upgrading does not re-pull history.
    """
    run = session.scalar(
        select(IngestionRun)
        .where(
            IngestionRun.job == JOB_NAME,
            IngestionRun.status.in_(COMPLETED_STATUSES),
            IngestionRun.errors["details"]["mode"].astext.in_(HISTORY_MODES),
        )
        .order_by(IngestionRun.id.desc())
        .limit(1)
    )
    details = run.errors.get("details") if run is not None and isinstance(run.errors, dict) else None
    if not isinstance(details, dict):
        return set(), set()
    if "psc_codes" not in details and "naics_codes" not in details:
        return set(current[0]), set(current[1])
    return (
        {str(code) for code in details.get("psc_codes") or []},
        {str(code) for code in details.get("naics_codes") or []},
    )


def _uncovered(codes: list[str], covered: set[str]) -> tuple[str, ...]:
    """Codes not already fetched. Search codes match prefixes, so a code under
    a covered prefix (R425 under R4) is covered."""
    return tuple(code for code in codes if not any(code.startswith(prefix) for prefix in covered))


def plan_details(plan: PullPlan, *, trigger: str | None = None) -> dict:
    """Run details for a network pull; ``last_completed_window_end`` and
    ``covered_codes`` read them back."""
    details: dict = {
        "mode": plan.mode,
        "date_type": plan.date_type,
        "window_start": plan.start.isoformat(),
        "window_end": plan.end.isoformat(),
        "psc_codes": list(plan.psc_codes),
        "naics_codes": list(plan.naics_codes),
    }
    if plan.backfill_psc_codes or plan.backfill_naics_codes:
        details["backfilled_psc_codes"] = list(plan.backfill_psc_codes)
        details["backfilled_naics_codes"] = list(plan.backfill_naics_codes)
    if trigger:
        details["trigger"] = trigger
    return details


def plan_pull(
    session: Session,
    *,
    today: date | None = None,
    force_backfill: bool = False,
    start: date | None = None,
    end: date | None = None,
    date_type: str | None = None,
) -> PullPlan:
    """Choose the 3-year backfill or the incremental last-modified window.

    An incremental plan also lists codes added since the last history pull;
    those get the 3-year lookback so a new watchlist has award history.
    """
    today = today or datetime.now(UTC).date()
    psc_now, naics_now = collect_watchlist_codes(session)
    psc_codes = tuple(psc_now)
    naics_codes = tuple(naics_now)
    if start is not None or end is not None:
        if start is None or end is None:
            raise ValueError("window start and end must be provided together")
        if end < start:
            raise ValueError("window end is before window start")
        chosen = date_type or "action_date"
        if chosen not in {"action_date", "last_modified_date"}:
            raise ValueError("date_type must be action_date or last_modified_date")
        return PullPlan(
            mode="explicit", date_type=chosen, start=start, end=end,
            psc_codes=psc_codes, naics_codes=naics_codes,
        )
    if force_backfill:
        window_start, window_end = default_lookback(today)
        return PullPlan(
            mode="backfill", date_type="action_date", start=window_start, end=window_end,
            psc_codes=psc_codes, naics_codes=naics_codes,
        )
    previous_end = last_completed_window_end(session)
    if previous_end is None:
        window_start, window_end = default_lookback(today)
        return PullPlan(
            mode="backfill", date_type="action_date", start=window_start, end=window_end,
            psc_codes=psc_codes, naics_codes=naics_codes,
        )
    window_end = today
    window_start = previous_end - timedelta(days=1)
    window_start = min(window_start, window_end)
    covered_psc, covered_naics = covered_codes(session, (psc_now, naics_now))
    new_psc = _uncovered(psc_now, covered_psc)
    new_naics = _uncovered(naics_now, covered_naics)
    lookback_start, lookback_end = default_lookback(today)
    return PullPlan(
        mode="incremental",
        date_type="last_modified_date",
        start=window_start,
        end=window_end,
        backfill_psc_codes=new_psc,
        backfill_naics_codes=new_naics,
        backfill_start=lookback_start if new_psc or new_naics else None,
        backfill_end=lookback_end if new_psc or new_naics else None,
        psc_codes=psc_codes,
        naics_codes=naics_codes,
    )


def _search_body(filters: dict, *, page: int, limit: int) -> dict:
    return {
        "subawards": False,
        "limit": limit,
        "page": page,
        "sort": "generated_internal_id",
        "order": "asc",
        "filters": filters,
        "fields": list(SEARCH_FIELDS),
    }


def _request_page(
    client: httpx.Client,
    body: dict,
    *,
    attempts: int,
    wait: wait_base | None,
) -> dict:
    try:
        response = request_with_retry(
            client,
            "POST",
            SEARCH_URL,
            attempts=attempts,
            wait=wait,
            json=body,
        )
    except httpx.TransportError as exc:
        raise UsaSpendingError(f"USAspending search request failed: {exc}") from exc
    if response.status_code != 200:
        detail = redact(response.text[:500])
        raise UsaSpendingError(f"USAspending search failed with HTTP {response.status_code}: {detail}")
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise UsaSpendingError("USAspending search returned a non-JSON body") from exc
    if not isinstance(payload, dict):
        raise UsaSpendingError("USAspending search returned a non-object body")
    return payload


def iter_search_pages(
    client: httpx.Client,
    filters: dict,
    *,
    limit: int = PAGE_LIMIT,
    attempts: int = 5,
    wait: wait_base | None = None,
) -> Iterator[list[dict]]:
    """Yield result pages until ``hasNext`` is false."""
    if limit < 1 or limit > PAGE_LIMIT:
        raise ValueError(f"USAspending limit must be from 1 to {PAGE_LIMIT}")
    previous_ids: tuple[str, ...] | None = None
    for page in range(1, MAX_PAGES + 1):
        payload = _request_page(
            client,
            _search_body(filters, page=page, limit=limit),
            attempts=attempts,
            wait=wait,
        )
        for message in payload.get("messages") or []:
            if isinstance(message, str) and message.strip():
                logger.info("usaspending_message %s", message.strip())
        rows = payload.get("results")
        if not isinstance(rows, list):
            raise UsaSpendingError("USAspending search payload missing results")
        objects = [row for row in rows if isinstance(row, dict)]
        page_ids = tuple(_text(row.get("generated_internal_id")) or "" for row in objects)
        if previous_ids and page_ids:
            overlap = len(set(previous_ids) & set(page_ids)) / len(page_ids)
            if overlap >= 0.5:
                raise UsaSpendingError(
                    "USAspending search page repeated award ids from the previous page. "
                    "The run stopped instead of truncating."
                )
        previous_ids = page_ids
        yield objects
        page_metadata = payload.get("page_metadata")
        meta = page_metadata if isinstance(page_metadata, dict) else {}
        if not objects or not meta.get("hasNext"):
            return
    raise UsaSpendingError(f"USAspending search exceeded {MAX_PAGES} pages")


def _record(stats: IngestStats, outcome: str) -> None:
    if outcome == "inserted":
        stats.inserted += 1
    elif outcome == "updated":
        stats.updated += 1
    else:
        stats.unchanged += 1


def ingest_award_records(session: Session, records: list[dict]) -> IngestStats:
    """Upsert award objects from a file or an already downloaded page."""
    stats = IngestStats()
    seen: set[str] = set()
    nsn_known = 0
    price_known = 0
    for payload in records:
        award_id = _text(payload.get("generated_internal_id"))
        if award_id is None:
            stats.errors.append("skipped award without generated_internal_id")
            continue
        if award_id in seen:
            continue
        seen.add(award_id)
        stats.fetched += 1
        try:
            outcome = upsert_award(session, payload)
        except ValueError as exc:
            stats.errors.append(f"{award_id}: {exc}")
            continue
        _record(stats, outcome)
        item = normalize_award(payload)
        if item is not None and item.nsn is not None:
            nsn_known += 1
        if item is not None and item.unit_price is not None:
            price_known += 1
    session.flush()
    logger.info(
        "usaspending_records fetched=%s inserted=%s updated=%s unchanged=%s nsn_known=%s unit_price_known=%s",
        stats.fetched,
        stats.inserted,
        stats.updated,
        stats.unchanged,
        nsn_known,
        price_known,
    )
    return stats


def _filters(plan: PullPlan, extra: dict) -> dict:
    return _window_filters(plan.start, plan.end, plan.date_type, extra)


def _window_filters(start: date, end: date, date_type: str, extra: dict) -> dict:
    return {
        "award_type_codes": list(CONTRACT_AWARD_TYPES),
        "time_period": [
            {
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "date_type": date_type,
            }
        ],
        **extra,
    }


def _code_families(psc_codes: list[str] | tuple[str, ...], naics_codes: list[str] | tuple[str, ...]) -> list[dict]:
    families: list[dict] = []
    if psc_codes:
        families.append({"psc_codes": list(psc_codes)})
    if naics_codes:
        families.append({"naics_codes": {"require": list(naics_codes)}})
    return families


def pull_usaspending(
    session: Session,
    plan: PullPlan,
    *,
    client: httpx.Client | None = None,
    settings: Settings | None = None,
    limit: int = PAGE_LIMIT,
    attempts: int = 5,
    wait: wait_base | None = None,
) -> IngestStats:
    """Pull contract awards for enabled watchlist PSC and NAICS codes.

    PSC and NAICS are requested separately. The search ANDs filters, and a
    watchlist area is the union of its codes.
    """
    settings = settings or get_settings()
    psc_codes, naics_codes = collect_watchlist_codes(session)
    if not psc_codes and not naics_codes:
        logger.info("usaspending_pull skipped=no_codes mode=%s", plan.mode)
        return IngestStats()
    searches = [_filters(plan, extra) for extra in _code_families(psc_codes, naics_codes)]
    if plan.backfill_start is not None and plan.backfill_end is not None:
        # Codes added since the last history pull get the full action-date lookback.
        searches += [
            _window_filters(plan.backfill_start, plan.backfill_end, "action_date", extra)
            for extra in _code_families(plan.backfill_psc_codes, plan.backfill_naics_codes)
        ]
    owns_client = client is None
    client = client or build_client(settings, timeout=60.0)
    stats = IngestStats()
    seen: set[str] = set()
    try:
        for filters in searches:
            for page in iter_search_pages(client, filters, limit=limit, attempts=attempts, wait=wait):
                fresh = []
                for row in page:
                    award_id = _text(row.get("generated_internal_id"))
                    if award_id is None:
                        stats.errors.append("skipped award without generated_internal_id")
                        continue
                    if award_id in seen:
                        continue
                    seen.add(award_id)
                    fresh.append(row)
                page_stats = ingest_award_records(session, fresh)
                stats.add(page_stats)
    except UsaSpendingError as exc:
        exc.stats = stats
        stats.errors.append(redact(str(exc)))
        raise
    finally:
        if owns_client:
            client.close()
    logger.info(
        "usaspending_pull mode=%s date_type=%s window=%s..%s psc=%s naics=%s fetched=%s inserted=%s updated=%s unchanged=%s",
        plan.mode,
        plan.date_type,
        plan.start.isoformat(),
        plan.end.isoformat(),
        len(psc_codes),
        len(naics_codes),
        stats.fetched,
        stats.inserted,
        stats.updated,
        stats.unchanged,
    )
    return stats


@dataclass
class ScheduledPullResult:
    run_id: int
    status: str
    stats: IngestStats
    details: dict


def ingest_usaspending_awards(
    session: Session,
    *,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
    today: date | None = None,
    attempts: int = 5,
    wait: wait_base | None = None,
) -> ScheduledPullResult:
    """Scheduled delta pull: plan the window, pull, and record the run.

    The run is recorded under ``JOB_NAME`` with ``details.mode`` and
    ``details.window_end`` so ``last_completed_window_end`` advances the
    incremental watermark exactly as the CLI does. Errors on individual
    records give ``completed_with_errors``; a failed search gives ``failed``.
    """
    from govcon.ingest.runs import finish_run, start_run

    settings = settings or get_settings()
    run = start_run(session, JOB_NAME)
    stats = IngestStats()
    details: dict = {}
    status = "succeeded"
    try:
        plan = plan_pull(session, today=today)
        details = plan_details(plan, trigger="scheduler")
        stats = pull_usaspending(session, plan, client=client, settings=settings, attempts=attempts, wait=wait)
    except (UsaSpendingError, OSError, ValueError) as exc:
        attached = getattr(exc, "stats", None)
        stats = attached if isinstance(attached, IngestStats) else stats
        message = redact(str(exc))
        if message not in stats.errors:
            stats.errors.append(message)
        status = "failed"
    if status != "failed" and stats.errors:
        status = "completed_with_errors"
    finish_run(run, stats, status=status, details=details or None)
    session.flush()
    return ScheduledPullResult(run_id=run.id, status=status, stats=stats, details=details)
