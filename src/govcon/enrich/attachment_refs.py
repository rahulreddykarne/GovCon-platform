"""One normalised attachment reference for every opportunity source.

The downloader accepts only ``AttachmentRef``. All source variants funnel
through ``attachment_refs_for``:

- SAM: ingest stores ``links["attachments"]`` as URL strings (from
  ``resourceLinks``, which may arrive as strings or objects);
- legacy rows: ``links["resourceLinks"]`` / ``links["attachments"]`` objects,
  and ``raw["resourceLinks"]`` strings or objects;
- source documents the feeds point to but do not carry
  (``govcon.enrich.source_documents``): every SAM notice's description
  (``links["description"]``), and the DIBBS RFQ PDF of a DLA solicitation,
  for DIBBS rows and for DLA notices on SAM that list no attachments of
  their own. The daily DIBBS package zip is not an attachment of one RFQ
  and is never fetched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from govcon.models import Opportunity

_URL_KEYS = ("url", "uri", "href", "link")
_NAME_KEYS = ("name", "filename", "fileName", "title")

SAM_DESCRIPTION = "sam_description"
DIBBS_RFQ_PDF = "dibbs_rfq_pdf"
DIBBS_PDF_ROOT = "https://dibbs2.bsm.dla.mil/Downloads/RFQ"
# SPE + 3-character activity + 2-digit fiscal year + instrument letter + 4 digits, e.g. SPE4A727T0080.
_DLA_SOLICITATION = re.compile(r"^SPE[0-9A-Z]{3}\d{2}[A-Z]\d{4}$")


def dibbs_pdf_url(solicitation_number: str | None) -> str | None:
    """Where DIBBS publishes a DLA solicitation's PDF, or None for other numbers."""
    number = (solicitation_number or "").strip().upper()
    if not _DLA_SOLICITATION.match(number):
        return None
    return f"{DIBBS_PDF_ROOT}/{number[-1]}/{number}.PDF"


@dataclass(frozen=True)
class AttachmentRef:
    url: str
    filename: str | None = None
    source_metadata: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)


def _http_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    scheme = urlsplit(text).scheme.lower()
    return text if scheme in {"http", "https"} else None


def ref_from_item(item: object, *, origin: str) -> AttachmentRef | None:
    """Normalise one string or object attachment entry."""
    if isinstance(item, str):
        url = _http_url(item)
        return AttachmentRef(url=url, source_metadata={"origin": origin}) if url else None
    if isinstance(item, dict):
        url = next((u for u in (_http_url(item.get(k)) for k in _URL_KEYS) if u), None)
        if url is None:
            return None
        name = next((str(item[k]).strip() for k in _NAME_KEYS if isinstance(item.get(k), str) and item[k].strip()), None)
        return AttachmentRef(url=url, filename=name, source_metadata={"origin": origin})
    return None


def resource_link_urls(raw_links: object) -> list[str]:
    """URL strings from a SAM ``resourceLinks`` value (string, list of strings or objects)."""
    items = raw_links if isinstance(raw_links, list) else [raw_links]
    urls: list[str] = []
    for item in items:
        ref = ref_from_item(item, origin="resourceLinks")
        if ref is not None and ref.url not in urls:
            urls.append(ref.url)
    return urls


def attachment_refs_for(opportunity: Opportunity) -> list[AttachmentRef]:
    """Every distinct attachment URL the source currently lists, in order."""
    links = opportunity.links if isinstance(opportunity.links, dict) else {}
    raw = opportunity.raw if isinstance(opportunity.raw, dict) else {}
    candidates: list[AttachmentRef] = []
    for origin, value in (
        ("links.attachments", links.get("attachments")),
        ("links.resourceLinks", links.get("resourceLinks")),
        ("raw.resourceLinks", raw.get("resourceLinks")),
    ):
        if value is None:
            continue
        for item in value if isinstance(value, list) else [value]:
            ref = ref_from_item(item, origin=origin)
            if ref is not None:
                candidates.append(ref)
    by_url: dict[str, AttachmentRef] = {}
    for ref in candidates:
        existing = by_url.get(ref.url)
        if existing is None or (existing.filename is None and ref.filename):
            by_url[ref.url] = ref
    refs = list(by_url.values())
    for ref in _source_document_refs(opportunity, links, has_attachments=bool(refs)):
        if ref.url not in by_url:
            refs.append(ref)
    return refs


def _source_document_refs(opportunity: Opportunity, links: dict, *, has_attachments: bool) -> list[AttachmentRef]:
    refs: list[AttachmentRef] = []
    if opportunity.source == "sam":
        description = _http_url(links.get("description"))
        if description:
            refs.append(AttachmentRef(url=description, filename="SAM notice description.txt",
                                      source_metadata={"origin": SAM_DESCRIPTION}))
    # A DLA notice on SAM points to DIBBS for the RFQ; one with its own SAM
    # attachments already carries its documents.
    if opportunity.source == "dibbs" or (opportunity.source == "sam" and not has_attachments):
        number = opportunity.solicitation_number
        url = dibbs_pdf_url(number)
        if url and number is not None:
            refs.append(AttachmentRef(url=url, filename=f"{number.strip().upper()}.pdf",
                                      source_metadata={"origin": DIBBS_RFQ_PDF}))
    return refs
