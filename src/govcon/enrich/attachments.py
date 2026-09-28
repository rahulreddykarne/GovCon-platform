"""Attachment download, deduplication, and text extraction for pursued/reviewing opportunities.

Downloads source attachments, computes SHA-256, preserves the original file,
extracts text, and tracks extraction status/errors. Identical content (same
SHA-256) is not re-downloaded or re-processed.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from govcon.config import Settings, get_settings
from govcon.enrich.extract import ExtractionResult, extract_text, guess_mime_type
from govcon.http import build_client, request_with_retry
from govcon.models import Opportunity, StoredFile

logger = logging.getLogger("govcon.enrich.attachments")

_DIBBS_DOCS_HOST = "dibbs2.bsm.dla.mil"
_DIBBS_MAIN_HOST = "dibbs.bsm.dla.mil"


def _is_dibbs_url(url: str) -> bool:
    """Return True when the URL points to a DIBBS document host that may serve a consent banner."""
    return _DIBBS_DOCS_HOST in url or _DIBBS_MAIN_HOST in url


def _dibbs_rfq_pdf_url(solicitation_number: str) -> str | None:
    """Derive the per-solicitation RFQ PDF URL from a DIBBS solicitation number.

    URL pattern: ``https://dibbs2.bsm.dla.mil/Downloads/RFQ/{last_letter}/{solicitation}.PDF``
    For example, SPE4A526T443K → .../Downloads/RFQ/K/SPE4A526T443K.PDF
    """
    sn = (solicitation_number or "").strip().upper()
    if not sn:
        return None
    last = sn[-1]
    if not last.isalpha():
        return None
    return f"https://{_DIBBS_DOCS_HOST}/Downloads/RFQ/{last}/{sn}.PDF"


def download_attachments(
    session: Session,
    opportunity: Opportunity,
    *,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
) -> list[StoredFile]:
    """Download and extract text from all attachments for an opportunity.

    Skips files already downloaded with the same URL and SHA-256.
    Returns a list of ``StoredFile`` rows (new and existing).
    """
    settings = settings or get_settings()
    urls = _collect_attachment_urls(opportunity)
    if not urls:
        logger.info("opportunity %s has no attachment URLs", opportunity.id)
        return []

    own_client = client is None
    if own_client:
        client = build_client(settings)
    try:
        results: list[StoredFile] = []
        for url, filename in urls:
            sf = _download_one(
                session, opportunity, url, filename,
                settings=settings, client=client,
            )
            if sf is not None:
                results.append(sf)
        return results
    finally:
        if own_client:
            client.close()


def _collect_attachment_urls(opp: Opportunity) -> list[tuple[str, str]]:
    """Extract attachment URLs and filenames from the opportunity's links/raw.

    For SAM opportunities the standard ``resourceLinks`` / ``attachments`` keys are used.
    For DIBBS opportunities the per-solicitation RFQ PDF is derived from the solicitation
    number using the known DIBBS document-host URL pattern.
    """
    urls: list[tuple[str, str]] = []
    links = opp.links or {}

    resource_links = links.get("resourceLinks") or []
    if isinstance(resource_links, list):
        for item in resource_links:
            if isinstance(item, dict):
                url = item.get("url") or item.get("uri") or ""
                name = item.get("name") or item.get("filename") or _url_filename(url)
                if url:
                    urls.append((url, name))
            elif isinstance(item, str) and item.startswith("http"):
                urls.append((item, _url_filename(item)))

    attachments = links.get("attachments") or []
    if isinstance(attachments, list):
        for item in attachments:
            if isinstance(item, dict):
                url = item.get("url") or item.get("uri") or ""
                name = item.get("name") or item.get("filename") or _url_filename(url)
                if url:
                    urls.append((url, name))

    raw = opp.raw or {}
    for rl in raw.get("resourceLinks", []):
        if isinstance(rl, dict):
            url = rl.get("url") or ""
            if url and not any(u == url for u, _ in urls):
                urls.append((url, _url_filename(url)))

    # DIBBS opportunities: derive the per-solicitation RFQ PDF URL from the
    # solicitation number.  The links dict stores the batch archive ("package")
    # but not the individual solicitation PDF, which follows the known pattern
    # https://dibbs2.bsm.dla.mil/Downloads/RFQ/{last_letter}/{solicitation}.PDF
    if getattr(opp, "source", None) == "dibbs":
        sol_num = opp.solicitation_number or (opp.raw or {}).get("solicitation_number")
        if sol_num:
            pdf_url = _dibbs_rfq_pdf_url(sol_num)
            if pdf_url and not any(u == pdf_url for u, _ in urls):
                filename = f"{sol_num.strip().upper()}.PDF"
                urls.append((pdf_url, filename))
                logger.debug("dibbs rfq_pdf_url derived solicitation=%s url=%s", sol_num, pdf_url)

    return urls


def _url_filename(url: str) -> str:
    """Extract a filename from the last path segment of a URL."""
    path = url.split("?")[0].split("#")[0]
    return path.rsplit("/", 1)[-1] or "attachment"


def _download_one(
    session: Session,
    opp: Opportunity,
    url: str,
    filename: str,
    *,
    settings: Settings,
    client: httpx.Client,
) -> StoredFile | None:
    """Download a single file, compute SHA-256, extract text, and persist.

    For DIBBS document-host URLs the DoD notice-and-consent banner is handled
    automatically via ``fetch_consented``.
    """
    try:
        if _is_dibbs_url(url):
            from govcon.ingest.dibbs import DibbsError, fetch_consented
            try:
                resp = fetch_consented(client, url, interval=settings.dibbs_request_interval_seconds)
            except DibbsError as exc:
                logger.warning("dibbs consent failed for %s: %s", url, exc)
                return None
        else:
            resp = request_with_retry(client, "GET", url)
        if resp.status_code != 200:
            logger.warning("download failed for %s: HTTP %d", url, resp.status_code)
            return None
    except Exception as exc:
        logger.warning("download error for %s: %s", url, exc)
        return None

    data = resp.content
    if len(data) > settings.attachment_max_mb * 1024 * 1024:
        logger.warning(
            "attachment %s exceeds %dMB limit (%d bytes)",
            url, settings.attachment_max_mb, len(data),
        )
        return None

    sha = hashlib.sha256(data).hexdigest()

    existing = session.execute(
        select(StoredFile).where(
            StoredFile.opportunity_id == opp.id,
            StoredFile.url == url,
            StoredFile.sha256 == sha,
        )
    ).scalar_one_or_none()
    if existing is not None:
        logger.debug("deduplicated %s (sha256 match)", url)
        return existing

    mime = guess_mime_type(filename)
    local_path = _save_file(data, opp.id, filename, settings)
    extraction = extract_text(data, mime, filename)

    sf = StoredFile(
        opportunity_id=opp.id,
        filename=filename,
        url=url,
        local_path=str(local_path) if local_path else None,
        mime_type=mime,
        sha256=sha,
        extracted_text=extraction.text,
        extraction_status=extraction.status,
        extraction_error=extraction.error,
    )
    session.add(sf)
    session.flush()
    return sf


def _save_file(data: bytes, opp_id: int, filename: str, settings: Settings) -> Path | None:
    """Write the original file bytes to the data directory."""
    try:
        dest_dir = settings.data_dir / "attachments" / str(opp_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename
        dest.write_bytes(data)
        return dest
    except Exception as exc:
        logger.warning("failed to save attachment locally: %s", exc)
        return None


def process_local_file(
    session: Session,
    opportunity: Opportunity,
    file_path: Path,
) -> StoredFile:
    """Process a local file: compute SHA-256, extract text, persist.

    Used for fixture-based and offline testing.
    """
    data = file_path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    filename = file_path.name
    mime = guess_mime_type(filename)
    extraction = extract_text(data, mime, filename)

    existing = session.execute(
        select(StoredFile).where(
            StoredFile.opportunity_id == opportunity.id,
            StoredFile.sha256 == sha,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    sf = StoredFile(
        opportunity_id=opportunity.id,
        filename=filename,
        url=None,
        local_path=str(file_path),
        mime_type=mime,
        sha256=sha,
        extracted_text=extraction.text,
        extraction_status=extraction.status,
        extraction_error=extraction.error,
    )
    session.add(sf)
    session.flush()
    return sf
