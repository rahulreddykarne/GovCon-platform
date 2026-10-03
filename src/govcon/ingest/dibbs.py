"""DIBBS daily index ingestion.

Verified 2026-09-26 against the live DIBBS sites:

- Application: https://www.dibbs.bsm.dla.mil/ (DIBBS 6.3.2)
- Documents host: https://dibbs2.bsm.dla.mil/
- Batch listing: https://www.dibbs.bsm.dla.mil/RFQ/RFQDates.aspx?category=recent
- File layout: https://www.dibbs.bsm.dla.mil/Rfq/RfqFileDefs.aspx

Each post date publishes three files. This job downloads and retains the
fixed-width index ``inYYMMDD.txt``. It does not download ``caYYMMDD.zip``
(per-solicitation PDF/HTML) or ``bqYYMMDD.zip`` (quote-upload template), and
it does not open individual RFQ HTML pages.

Index columns (widths sum to 140, no delimiter):

- Solicitation # (13)
- NSN/Part # (46)
- Purchase Request # (13)
- Return By Date (8, ``MM/DD/YY``)
- File Name (19)
- QTY (7)
- Unit Issue (2)
- Nomenclature (21)
- Buyer Code (5)
- AMSC (1)
- Item type (1): ``1`` NSN, ``2`` part number
- Small-business set-aside (1): Y, H, R, L, A, E, N
- Set-aside percentage (3)

Return-by time: quotes are due at 3:00 PM Eastern (EST or EDT) on the return
date, and a return date on a Saturday, Sunday, or federal holiday moves to the
next business day (DLA Master Solicitation, "Time for receipt of quotes"). The
stored ``response_deadline`` is that instant in UTC.

Catch-up: the scheduled pull ingests every index the recent page lists that is
newer than the last one ingested (oldest first, at most
``MAX_CATCHUP_INDEXES``), so a missed day is picked up. With no DIBBS history
only the newest index is pulled.

A DoD notice-and-consent banner gates both hosts. ``/robots.txt`` is not
published (www returns 404 after consent; dibbs2 returns its file-not-found
page). Requests are sequential and wait ``DIBBS_REQUEST_INTERVAL_SECONDS``
between calls.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tenacity import wait_exponential

from govcon.config import Settings, get_settings
from govcon.http import build_client, request_with_retry
from govcon.ingest.runs import IngestStats
from govcon.ingest.snapshots import (
    NormalizedOpportunity,
    canonical_content_hash,
    description_hash,
    upsert_opportunity,
)
from govcon.models import IngestionRun, Opportunity

logger = logging.getLogger("govcon.ingest.dibbs")

SOURCE_DIBBS = "dibbs"
AGENCY_PATH = "Defense Logistics Agency"
RECENT_RFQ_URL = "https://www.dibbs.bsm.dla.mil/RFQ/RFQDates.aspx?category=recent"
ARCHIVE_ROOT = "https://dibbs2.bsm.dla.mil/Downloads/RFQ/Archive"
RECORD_URL = "https://www.dibbs.bsm.dla.mil/RFQ/RfqRec.aspx?sn={solicitation}"
INDEX_RECORD_LENGTH = 140
DIBBS_RETRY_ATTEMPTS = 3
DIBBS_RETRY_WAIT = wait_exponential(multiplier=1, min=1, max=8)
EASTERN = ZoneInfo("America/New_York")
QUOTES_DUE_LOCAL_TIME = clock_time(15, 0)
MAX_CATCHUP_INDEXES = 14
# Ingestion-run job names whose ``details.indexes`` record pulled index files.
DIBBS_RUN_JOBS = ("sched:dibbs_ingest", "dibbs_index")

INDEX_FIELDS: tuple[tuple[str, int], ...] = (
    ("solicitation_number", 13),
    ("nsn_or_part", 46),
    ("purchase_request", 13),
    ("return_by", 8),
    ("file_name", 19),
    ("quantity", 7),
    ("unit", 2),
    ("nomenclature", 21),
    ("buyer_code", 5),
    ("amsc", 1),
    ("item_type", 1),
    ("set_aside", 1),
    ("set_aside_percent", 3),
)

SET_ASIDE_LABELS = {
    "Y": "Small Business Set-Aside",
    "H": "HUBZone Set-Aside",
    "R": "Service Disabled Veteran-Owned Small Business Set-Aside",
    "L": "Woman Owned Small Business Set-Aside",
    "A": "8(a) Set-Aside",
    "E": "Economically Disadvantaged Woman Owned Small Business Set-Aside",
    "N": "Unrestricted/Not Set-Aside",
}
ITEM_TYPE_LABELS = {"1": "NSN", "2": "Part Number"}

_INDEX_NAME = re.compile(r"^in(\d{6})\.txt$", re.IGNORECASE)
_INDEX_LINK = re.compile(
    r"https://dibbs2\.bsm\.dla\.mil/Downloads/RFQ/Archive/in\d{6}\.txt",
    re.IGNORECASE,
)

if sum(width for _, width in INDEX_FIELDS) != INDEX_RECORD_LENGTH:
    raise RuntimeError("DIBBS index column widths must sum to 140")


class DibbsError(RuntimeError):
    """A DIBBS batch file could not be downloaded or read."""


@dataclass
class DibbsCoverage:
    records: int
    nsn: int
    quantity: int

    @property
    def nsn_ratio(self) -> float:
        if self.records == 0:
            return 0.0
        return self.nsn / self.records

    @property
    def quantity_ratio(self) -> float:
        if self.records == 0:
            return 0.0
        return self.quantity / self.records


@dataclass
class DibbsIngestResult:
    stats: IngestStats
    coverage: DibbsCoverage
    index_name: str
    retained_path: str | None = None
    # Every index file ingested by this call, oldest first.
    index_names: list[str] | None = None

    def run_details(self) -> dict:
        return {"indexes": list(self.index_names or [self.index_name])}


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.action: str | None = None
        self.inputs: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form" and self.action is None:
            self.action = values.get("action")
        if tag == "input":
            name = values.get("name")
            if name:
                self.inputs[name] = values.get("value") or ""


def parse_user_date(value: str) -> date:
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid date {value!r}. Use YYYY-MM-DD or MM/dd/yyyy.")


def posted_date_from_name(name: str) -> date | None:
    match = _INDEX_NAME.match(Path(name).name)
    if match is None:
        return None
    return datetime.strptime(match.group(1), "%y%m%d").replace(tzinfo=UTC).date()


def index_file_url(posted: date) -> str:
    return f"{ARCHIVE_ROOT}/in{posted.strftime('%y%m%d')}.txt"


def index_links(html: str) -> list[str]:
    """Index file URLs in document order. The recent page lists the newest first."""
    found: list[str] = []
    seen: set[str] = set()
    for match in _INDEX_LINK.findall(html):
        key = match.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(match)
    return found


def _slice_fields(line: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    cursor = 0
    for name, width in INDEX_FIELDS:
        fields[name] = line[cursor : cursor + width].strip()
        cursor += width
    return fields


def _nsn(item_type: str, nsn_or_part: str) -> str | None:
    """Dash-group a 13-digit NSN. Part numbers and local stock numbers stay unset."""
    if item_type != "1":
        return None
    if len(nsn_or_part) == 13 and nsn_or_part.isdigit():
        return f"{nsn_or_part[0:4]}-{nsn_or_part[4:6]}-{nsn_or_part[6:9]}-{nsn_or_part[9:13]}"
    return None


def _quantity(text: str) -> Decimal | None:
    if not text or not text.isdigit():
        return None
    return Decimal(int(text))


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The ``n``-th ``weekday`` (Mon=0) of a month; ``n=-1`` is the last one."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    following = date(year + (month == 12), month % 12 + 1, 1)
    last = following - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def federal_holidays(year: int) -> set[date]:
    """US federal holidays (5 U.S.C. 6103) as observed, for one calendar year."""
    fixed = [date(year, 1, 1), date(year, 6, 19), date(year, 7, 4), date(year, 11, 11), date(year, 12, 25)]
    days = {_observed(day) for day in fixed}
    days |= {
        _nth_weekday(year, 1, 0, 3),  # Birthday of Martin Luther King, Jr.
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _nth_weekday(year, 5, 0, -1),  # Memorial Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 10, 0, 2),  # Columbus Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving Day
    }
    # New Year's Day of the next year can be observed on Dec 31.
    days.add(_observed(date(year + 1, 1, 1)))
    return days


def next_business_day(day: date) -> date:
    """``day`` itself when it is a business day, else the next one."""
    while day.weekday() >= 5 or day in federal_holidays(day.year):
        day += timedelta(days=1)
    return day


def dibbs_return_deadline(day: date) -> datetime:
    """3:00 PM Eastern on the business-day return date, as an aware UTC datetime."""
    local = datetime.combine(next_business_day(day), QUOTES_DUE_LOCAL_TIME, tzinfo=EASTERN)
    return local.astimezone(UTC)


def _return_deadline(text: str) -> datetime | None:
    if not text:
        return None
    try:
        day = datetime.strptime(text, "%m/%d/%y").replace(tzinfo=UTC).date()
    except ValueError:
        return None
    return dibbs_return_deadline(day)


def _source_id(solicitation: str, purchase_request: str, nsn_or_part: str, line_number: int) -> str:
    if purchase_request:
        return f"{solicitation}:{purchase_request}"
    tail = nsn_or_part or str(line_number)
    return f"{solicitation}:{tail}"


def _record_links(solicitation: str, index_name: str, file_name: str) -> dict[str, str]:
    links = {"ui": RECORD_URL.format(solicitation=solicitation)}
    if file_name:
        links["document_name"] = file_name
    posted = posted_date_from_name(index_name)
    if posted is not None:
        stamp = posted.strftime("%y%m%d")
        links["index"] = f"{ARCHIVE_ROOT}/in{stamp}.txt"
        links["package"] = f"{ARCHIVE_ROOT}/ca{stamp}.zip"
        links["batch_quote"] = f"{ARCHIVE_ROOT}/bq{stamp}.zip"
        links["posted_date_search"] = (
            "https://www.dibbs.bsm.dla.mil/RFQ/RfqRecs.aspx?category=post&TypeSrch=dt&Value="
            + posted.strftime("%m-%d-%Y")
        )
    return links


def parse_index(text: str, *, index_name: str) -> tuple[list[NormalizedOpportunity], list[str]]:
    """Parse a fixed-width DIBBS index. Short lines are skipped and reported."""
    items: list[NormalizedOpportunity] = []
    errors: list[str] = []
    posted = posted_date_from_name(index_name)
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        if len(line) != INDEX_RECORD_LENGTH:
            errors.append(f"line {line_number}: expected {INDEX_RECORD_LENGTH} characters, got {len(line)}")
            continue
        fields = _slice_fields(line)
        solicitation = fields["solicitation_number"]
        if not solicitation:
            errors.append(f"line {line_number}: missing solicitation number")
            continue
        if fields["return_by"] and _return_deadline(fields["return_by"]) is None:
            errors.append(f"line {line_number}: return-by date {fields['return_by']!r} is not MM/DD/YY")
        if fields["quantity"] and _quantity(fields["quantity"]) is None:
            errors.append(f"line {line_number}: quantity {fields['quantity']!r} is not numeric")
        items.append(_to_opportunity(line, fields, line_number=line_number, index_name=index_name, posted=posted))
    return items, errors


def _to_opportunity(
    line: str,
    fields: dict[str, str],
    *,
    line_number: int,
    index_name: str,
    posted: date | None,
) -> NormalizedOpportunity:
    nsn = _nsn(fields["item_type"], fields["nsn_or_part"])
    quantity = _quantity(fields["quantity"])
    title = fields["nomenclature"] or None
    deadline = _return_deadline(fields["return_by"])
    set_aside = fields["set_aside"] or None
    buyer_code = fields["buyer_code"] or None
    amsc = fields["amsc"] or None
    raw = {
        "record": line,
        "solicitation_number": fields["solicitation_number"],
        "nsn_or_part": fields["nsn_or_part"],
        "purchase_request": fields["purchase_request"],
        "return_by": fields["return_by"],
        "file_name": fields["file_name"],
        "quantity": fields["quantity"],
        "unit": fields["unit"],
        "nomenclature": fields["nomenclature"],
        "buyer_code": fields["buyer_code"],
        "amsc": fields["amsc"],
        "item_type": fields["item_type"],
        "item_type_label": ITEM_TYPE_LABELS.get(fields["item_type"]),
        "set_aside": fields["set_aside"],
        "set_aside_label": SET_ASIDE_LABELS.get(fields["set_aside"]),
        "set_aside_percent": fields["set_aside_percent"],
    }
    poc: list[dict[str, str]] | None = None
    if buyer_code or amsc:
        contact: dict[str, str] = {"contact_type": "buyer"}
        if buyer_code:
            contact["buyer_code"] = buyer_code
        if amsc:
            contact["amsc"] = amsc
        poc = [contact]
    return NormalizedOpportunity(
        source=SOURCE_DIBBS,
        source_id=_source_id(fields["solicitation_number"], fields["purchase_request"], fields["nsn_or_part"], line_number),
        solicitation_number=fields["solicitation_number"],
        title=title,
        description=title,
        description_hash=description_hash(title),
        opportunity_type="RFQ",
        psc_code=nsn[:4] if nsn else None,
        naics_code=None,
        set_aside_code=set_aside,
        agency_path=AGENCY_PATH,
        place_of_performance=None,
        nsn=nsn,
        nsn_candidates=(nsn,) if nsn else (),
        quantity=quantity,
        unit=fields["unit"] or None,
        estimated_value_min=None,
        estimated_value_max=None,
        estimated_value_source=None,
        posted_date=posted,
        response_deadline=deadline,
        archive_date=None,
        status="open",
        poc=poc,
        links=_record_links(fields["solicitation_number"], index_name, fields["file_name"]),
        raw=raw,
        content_hash=canonical_content_hash(raw),
        source_version=Path(index_name).name,
        contacts=(),
    )


def _log_coverage(index_name: str, coverage: DibbsCoverage) -> None:
    logger.info(
        "dibbs_coverage index=%s records=%s nsn=%s quantity=%s nsn_coverage=%.4f quantity_coverage=%.4f",
        index_name,
        coverage.records,
        coverage.nsn,
        coverage.quantity,
        coverage.nsn_ratio,
        coverage.quantity_ratio,
    )


def ingest_index_bytes(session: Session, payload: bytes, *, index_name: str) -> DibbsIngestResult:
    """Upsert every parsed index line. Unchanged records do not add snapshots."""
    items, errors = parse_index(payload.decode("latin-1"), index_name=index_name)
    stats = IngestStats(fetched=len(items), errors=list(errors))
    for item in items:
        outcome = upsert_opportunity(session, item)
        if outcome == "inserted":
            stats.inserted += 1
        elif outcome == "updated":
            stats.updated += 1
        else:
            stats.unchanged += 1
    coverage = DibbsCoverage(
        records=len(items),
        nsn=sum(1 for item in items if item.nsn),
        quantity=sum(1 for item in items if item.quantity is not None),
    )
    _log_coverage(index_name, coverage)
    return DibbsIngestResult(stats=stats, coverage=coverage, index_name=Path(index_name).name)


def retain_batch_file(data_dir: Path, index_name: str, payload: bytes) -> Path:
    directory = data_dir / "dibbs"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / Path(index_name).name
    path.write_bytes(payload)
    return path


def ingest_index_file(session: Session, path: Path, *, data_dir: Path) -> DibbsIngestResult:
    payload = path.read_bytes()
    index_name = path.name.lower() if _INDEX_NAME.match(path.name) else path.name
    result = ingest_index_bytes(session, payload, index_name=index_name)
    result.retained_path = str(retain_batch_file(data_dir, index_name, payload))
    return result


def _body_text(response: httpx.Response) -> str:
    return response.content.decode("latin-1", errors="replace")


def _is_warning(text: str) -> bool:
    return "butAgree" in text and "Notice and Consent" in text


def _is_missing(text: str) -> bool:
    return "File was not found" in text or "The resource you are looking for" in text


def _sleep(interval: float) -> None:
    if interval > 0:
        time.sleep(interval)


def fetch_consented(client: httpx.Client, url: str, *, interval: float) -> httpx.Response:
    """GET a DIBBS URL, accepting the notice-and-consent banner when it is shown."""
    response = request_with_retry(client, "GET", url, attempts=DIBBS_RETRY_ATTEMPTS, wait=DIBBS_RETRY_WAIT)
    text = _body_text(response)
    if not _is_warning(text):
        return response
    parser = _FormParser()
    parser.feed(text)
    if not parser.action:
        raise DibbsError("DIBBS consent page did not include a form action")
    fields = dict(parser.inputs)
    fields["butAgree"] = "OK"
    _sleep(interval)
    posted = request_with_retry(
        client,
        "POST",
        urljoin(str(response.url), parser.action),
        attempts=DIBBS_RETRY_ATTEMPTS,
        wait=DIBBS_RETRY_WAIT,
        data=fields,
    )
    if _is_warning(_body_text(posted)):
        raise DibbsError("DIBBS consent banner was not accepted")
    return posted


def _payload_is_index(payload: bytes) -> bool:
    text = payload.decode("latin-1", errors="replace")
    if _is_missing(text) or _is_warning(text):
        return False
    for line in text.splitlines():
        if not line.strip():
            continue
        return len(line) == INDEX_RECORD_LENGTH and not line.lstrip().startswith("<")
    return False


def last_ingested_index_date(session: Session) -> date | None:
    """Newest DIBBS index already ingested, from run records and stored rows."""
    dates: list[date] = []
    newest_row = session.scalar(
        select(func.max(Opportunity.posted_date)).where(Opportunity.source == SOURCE_DIBBS)
    )
    if newest_row is not None:
        dates.append(newest_row)
    runs = session.scalars(
        select(IngestionRun.errors).where(
            IngestionRun.job.in_(DIBBS_RUN_JOBS),
            IngestionRun.status.in_(("succeeded", "completed_with_errors")),
        )
    ).all()
    for payload in runs:
        details = payload.get("details") if isinstance(payload, dict) else None
        for name in (details or {}).get("indexes") or []:
            posted = posted_date_from_name(str(name))
            if posted is not None:
                dates.append(posted)
    return max(dates) if dates else None


def _catchup_urls(links: list[str], last: date | None) -> list[str]:
    """Listed indexes newer than ``last``, oldest first; the newest alone when none are."""
    dated: list[tuple[date, str]] = []
    for url in links:
        posted = posted_date_from_name(Path(urlparse(url).path).name)
        if posted is not None:
            dated.append((posted, url))
    if not dated:
        return links[:1]
    newest = max(dated)[1]
    if last is None:
        return [newest]
    newer = sorted((posted, url) for posted, url in dated if posted > last)
    if not newer:
        # Re-pull the newest: idempotent, and it picks up same-day revisions.
        return [newest]
    return [url for _, url in newer[-MAX_CATCHUP_INDEXES:]]


def _pull_one(session: Session, client: httpx.Client, url: str, *, settings: Settings, interval: float) -> DibbsIngestResult:
    response = fetch_consented(client, url, interval=interval)
    payload = response.content
    if not _payload_is_index(payload):
        raise DibbsError(f"DIBBS index file was not available at {url}")
    index_name = Path(urlparse(url).path).name.lower()
    result = ingest_index_bytes(session, payload, index_name=index_name)
    result.retained_path = str(retain_batch_file(settings.data_dir, index_name, payload))
    return result


def pull_dibbs_index(
    session: Session,
    *,
    settings: Settings | None = None,
    posted_date: date | None = None,
    client: httpx.Client | None = None,
    interval: float | None = None,
) -> DibbsIngestResult:
    """Download and ingest DIBBS indexes.

    With ``posted_date`` exactly that index is pulled. Otherwise every listed
    index newer than the last one ingested is pulled, oldest first (see the
    module docstring). An unavailable index among several is reported in the
    stats and the rest are still ingested; it fails only when none could be.
    """
    settings = settings or get_settings()
    interval = settings.dibbs_request_interval_seconds if interval is None else interval
    own_client = client is None
    client = client or build_client(settings, timeout=60.0)
    try:
        if posted_date is None:
            listing = fetch_consented(client, RECENT_RFQ_URL, interval=interval)
            html = _body_text(listing)
            if _is_missing(html) or _is_warning(html):
                raise DibbsError("DIBBS recent RFQ page was not available")
            links = index_links(html)
            if not links:
                raise DibbsError("DIBBS recent RFQ page did not list an index file")
            urls = _catchup_urls(links, last_ingested_index_date(session))
            _sleep(interval)
        else:
            urls = [index_file_url(posted_date)]

        results: list[DibbsIngestResult] = []
        failures: list[str] = []
        for position, url in enumerate(urls):
            if position:
                _sleep(interval)
            try:
                results.append(_pull_one(session, client, url, settings=settings, interval=interval))
            except DibbsError as exc:
                if len(urls) == 1:
                    raise
                failures.append(str(exc))
        if not results:
            raise DibbsError("; ".join(failures) or "no DIBBS index could be ingested")
        return _combine(results, failures)
    finally:
        if own_client:
            client.close()


def _combine(results: list[DibbsIngestResult], failures: list[str]) -> DibbsIngestResult:
    if len(results) == 1 and not failures:
        only = results[0]
        only.index_names = [only.index_name]
        return only
    stats = IngestStats()
    for result in results:
        stats.add(result.stats)
    stats.errors.extend(failures)
    coverage = DibbsCoverage(
        records=sum(r.coverage.records for r in results),
        nsn=sum(r.coverage.nsn for r in results),
        quantity=sum(r.coverage.quantity for r in results),
    )
    newest = results[-1]
    return DibbsIngestResult(
        stats=stats,
        coverage=coverage,
        index_name=newest.index_name,
        retained_path=newest.retained_path,
        index_names=[r.index_name for r in results],
    )


