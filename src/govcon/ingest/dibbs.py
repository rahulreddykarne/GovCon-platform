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
from datetime import date, datetime, timezone
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy.orm import Session
from tenacity import wait_exponential

from govcon.config import Settings, get_settings
from govcon.http import build_client, request_with_retry
from govcon.ingest.runs import IngestStats
from govcon.ingest.status import opportunity_status
from govcon.ingest.snapshots import (
    NormalizedOpportunity,
    canonical_content_hash,
    description_hash,
    upsert_opportunity,
)

logger = logging.getLogger("govcon.ingest.dibbs")

SOURCE_DIBBS = "dibbs"
AGENCY_PATH = "Defense Logistics Agency"
RECENT_RFQ_URL = "https://www.dibbs.bsm.dla.mil/RFQ/RFQDates.aspx?category=recent"
ARCHIVE_ROOT = "https://dibbs2.bsm.dla.mil/Downloads/RFQ/Archive"
RECORD_URL = "https://www.dibbs.bsm.dla.mil/RFQ/RfqRec.aspx?sn={solicitation}"
INDEX_RECORD_LENGTH = 140
DIBBS_RETRY_ATTEMPTS = 3
DIBBS_RETRY_WAIT = wait_exponential(multiplier=1, min=1, max=8)

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
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid date {value!r}. Use YYYY-MM-DD or MM/dd/yyyy.")


def posted_date_from_name(name: str) -> date | None:
    match = _INDEX_NAME.match(Path(name).name)
    if match is None:
        return None
    return datetime.strptime(match.group(1), "%y%m%d").date()


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


def _return_deadline(text: str) -> datetime | None:
    if not text:
        return None
    try:
        day = datetime.strptime(text, "%m/%d/%y").date()
    except ValueError:
        return None
    return datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=timezone.utc)


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
        status=opportunity_status(
            source=SOURCE_DIBBS,
            notice_type="RFQ",
            active=None,
            response_deadline=deadline,
            archive_date=None,
        ),
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


def pull_dibbs_index(
    session: Session,
    *,
    settings: Settings | None = None,
    posted_date: date | None = None,
    client: httpx.Client | None = None,
    interval: float | None = None,
) -> DibbsIngestResult:
    """Download one daily index and ingest it. The default date is the newest listed file."""
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
            url = links[0]
            _sleep(interval)
        else:
            url = index_file_url(posted_date)
        response = fetch_consented(client, url, interval=interval)
        payload = response.content
        if not _payload_is_index(payload):
            raise DibbsError(f"DIBBS index file was not available at {url}")
        index_name = Path(urlparse(url).path).name.lower()
        result = ingest_index_bytes(session, payload, index_name=index_name)
        result.retained_path = str(retain_batch_file(settings.data_dir, index_name, payload))
        return result
    finally:
        if own_client:
            client.close()
