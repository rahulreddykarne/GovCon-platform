"""Solicitation text the feeds point to but do not carry.

- **SAM notice description.** The search API gives only a ``noticedesc`` URL;
  its JSON body (``{"description": "<html>"}``) is the notice text, and for
  most DLA notices the only text SAM has. It is stored as a plain-text
  document.
- **DIBBS RFQ PDF.** Each DLA solicitation's PDF is published on DIBBS at
  ``Downloads/RFQ/<last character>/<solicitation>.PDF``, behind the DoD
  notice-and-consent banner. Before consent DIBBS answers every address,
  even a missing one, with ``200`` and the banner page, so the bytes must
  be a PDF to count as the solicitation.

Verified 2026-10-05: ``noticedesc`` returns ``application/hal+json`` with a
``description`` string; DIBBS returns ``application/pdf`` after consent and
404 for a solicitation it does not hold.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx

from govcon.enrich.safe_fetch import (
    FetchBlocked,
    FetchError,
    FetchResult,
    FetchTooLarge,
)

DIBBS_HOST = "dibbs2.bsm.dla.mil"
_BLOCK_TAGS = frozenset({"p", "div", "br", "li", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol"})


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(markup: str) -> str:
    """Readable text from a notice's HTML: tags dropped, entities decoded, one line per block."""
    parser = _TextParser()
    parser.feed(markup)
    parser.close()
    lines = (re.sub(r"[ \t\xa0]+", " ", line).strip() for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)


def sam_description_text(payload: bytes) -> str:
    """The plain text of a SAM ``noticedesc`` response."""
    try:
        body = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FetchError("SAM returned an unreadable notice description") from exc
    description = body.get("description") if isinstance(body, dict) else None
    if not isinstance(description, str):
        raise FetchError("SAM returned no notice description")
    return html_to_text(description)


def fetch_dibbs_pdf(client: httpx.Client, url: str, *, max_bytes: int, interval: float) -> FetchResult:
    """GET one DIBBS RFQ PDF, accepting the consent banner; anything but PDF bytes is a failure.

    The address is built by GovCon from the solicitation number, never taken
    from the feed, and only the DIBBS download host is allowed. DIBBS PDFs are
    small (hundreds of KB), so the body is read whole and then checked
    against ``max_bytes``.
    """
    from govcon.ingest.dibbs import DibbsError, fetch_consented

    parts = urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "").lower() != DIBBS_HOST:
        raise FetchBlocked(f"not a DIBBS download address: {parts.hostname}")
    try:
        response = fetch_consented(client, url, interval=interval)
    except (DibbsError, httpx.HTTPError) as exc:
        raise FetchError(f"DIBBS request failed: {type(exc).__name__}") from exc
    if response.status_code == 404:
        raise FetchError("DIBBS has no solicitation PDF at this address (404); the RFQ may be closed or not posted yet")
    if response.status_code >= 400:
        raise FetchError(f"DIBBS returned HTTP {response.status_code}")
    data = response.content
    if len(data) > max_bytes:
        raise FetchTooLarge(f"DIBBS PDF exceeds {max_bytes} bytes")
    if not data.startswith(b"%PDF-"):
        raise FetchError("DIBBS returned a web page instead of the solicitation PDF")
    return FetchResult(
        final_url=str(response.url),
        status_code=response.status_code,
        content=data,
        content_type="application/pdf",
        content_disposition=response.headers.get("content-disposition"),
    )
