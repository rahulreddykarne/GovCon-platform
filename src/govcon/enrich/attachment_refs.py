"""One normalised attachment reference for every opportunity source.

The downloader accepts only ``AttachmentRef``. All source variants funnel
through ``attachment_refs_for``:

- SAM: ingest stores ``links["attachments"]`` as URL strings (from
  ``resourceLinks``, which may arrive as strings or objects);
- legacy rows: ``links["resourceLinks"]`` / ``links["attachments"]`` objects,
  and ``raw["resourceLinks"]`` strings or objects;
- DIBBS: the daily index carries no per-solicitation document URL (DEV-002);
  the shared daily package zip is not an attachment of one RFQ, so DIBBS
  opportunities yield no refs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from govcon.models import Opportunity

_URL_KEYS = ("url", "uri", "href", "link")
_NAME_KEYS = ("name", "filename", "fileName", "title")


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
    return list(by_url.values())
