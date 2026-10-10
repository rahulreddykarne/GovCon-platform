"""Attachment download and text extraction.

The downloader accepts only normalised ``AttachmentRef`` values
(``attachment_refs_for``). For each ref it:

- fetches through ``safe_fetch`` (HTTPS, public addresses only on every
  redirect, streamed with a byte cap); the SAM ``api_key`` is added only for
  SAM API hosts and never stored or logged. A DIBBS RFQ PDF is fetched
  through the consent banner and must be PDF bytes; a SAM notice
  description is unwrapped from its JSON into plain text
  (``govcon.enrich.source_documents``);
- names the file from ``Content-Disposition`` (else the ref or URL), types it
  from ``Content-Type``, reduces the name to a safe basename, and stores the
  bytes content-addressed inside the opportunity folder, written atomically
  and never overwriting;
- records provenance (``snapshot_id``, ``downloaded_at``) on every row, and
  stores a failed download as a ``download_failed`` row so the compliance
  inventory reports the attachment as missing. This holds even when an
  earlier version of the URL is stored: that version stays visible as the last
  known content, but the failure blocks the inventory until a fetch succeeds;
- reconciles attachment versions: for each URL the current source lists, the
  version this fetch produced is the ``active`` one (see
  ``reconcile_attachment_versions``).
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from sqlalchemy import delete, desc, select
from sqlalchemy.orm import Session

from govcon.config import Settings, get_settings
from govcon.enrich.attachment_refs import (
    DIBBS_RFQ_PDF,
    SAM_DESCRIPTION,
    AttachmentRef,
    attachment_refs_for,
)
from govcon.enrich.extract import ExtractionResult, extract_text, guess_mime_type
from govcon.enrich.safe_fetch import FetchError, Resolver, safe_fetch
from govcon.http import build_client
from govcon.ingest.snapshots import current_snapshot_id
from govcon.logging import redact
from govcon.models import FilePage, Opportunity, StoredFile
from govcon.security.classification import DataClassification, strictest_classification

logger = logging.getLogger("govcon.enrich.attachments")

DOWNLOAD_FAILED = "download_failed"
SAM_API_HOSTS = frozenset({"api.sam.gov"})
_GENERIC_NAMES = frozenset({"", "download", "file", "attachment", "content"})
_MAX_NAME = 120


def download_attachments(
    session: Session,
    opportunity: Opportunity,
    *,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
    resolver: Resolver | None = None,
) -> list[StoredFile]:
    """Download every attachment the source currently lists.

    Returns one ``StoredFile`` per ref: the stored version, or a
    ``download_failed`` row. Afterwards only listed versions stay active.
    """
    settings = settings or get_settings()
    refs = attachment_refs_for(opportunity)
    snapshot_id = latest_snapshot_id(session, opportunity.id)
    reused = current_sam_versions(session, opportunity.id, refs, snapshot_id)
    for row_id in reused.values():
        row = session.get(StoredFile, row_id)
        if row is not None and needs_ocr_retry(row, settings) and row.local_path:
            from govcon.enrich.ocr import ocr_config
            from govcon.enrich.storage import get_store

            data = get_store(settings).read(row.local_path)
            if data is not None and hashlib.sha256(data).hexdigest() == row.sha256:
                update_extraction(session, row, extract_text(
                    data, row.mime_type or "", row.filename or "", ocr=ocr_config(settings)
                ))
    current: dict[str, int] = dict(reused)
    to_fetch = [ref for ref in refs if ref.url not in reused]
    if to_fetch:
        own_client = client is None
        client = client or build_client(settings, timeout=60.0)
        try:
            for ref in to_fetch:
                row = _download_one(session, opportunity, ref, settings=settings, client=client,
                                    resolver=resolver, snapshot_id=snapshot_id)
                current[ref.url] = row.id
        finally:
            if own_client:
                client.close()
    elif not refs:
        logger.info("opportunity %s lists no attachments", opportunity.id)
    reconcile_attachment_versions(session, opportunity, refs, current=current)
    files: list[StoredFile] = []
    for ref in refs:
        stored = session.get(StoredFile, current[ref.url])
        assert stored is not None, "attachment disappeared during reconciliation"
        files.append(stored)
    return files


def latest_snapshot_id(session: Session, opportunity_id: int) -> int | None:
    """The snapshot the live row's content came from (handles A -> B -> A reverts)."""
    return current_snapshot_id(session, opportunity_id)


def reconcile_attachment_versions(
    session: Session,
    opportunity: Opportunity,
    refs: list[AttachmentRef] | None = None,
    *,
    current: dict[str, int] | None = None,
) -> int:
    """Keep only the versions the current source lists active. Returns rows deactivated.

    ``current`` maps each fetched URL to the row the fetch produced: the
    version whose bytes were just retrieved (even an older one, e.g. after an
    A -> B -> A revert) or the ``download_failed`` row.

    - A successful fetch: that version is the only active row for the URL.
    - A failed fetch: the failure row is active, so the inventory blocks until
      a fetch succeeds; the version that was active stays active as the last
      known content (it is not reported as removed or replaced).
    - A listed URL that was not fetched keeps its existing active state (no
      other version is promoted). Unlisted URLs become inactive.

    Local files (no URL) are not managed here.
    """
    refs = attachment_refs_for(opportunity) if refs is None else refs
    listed = {ref.url for ref in refs}
    current = current or {}
    rows = session.scalars(
        select(StoredFile)
        .where(StoredFile.opportunity_id == opportunity.id, StoredFile.url.is_not(None))
        .order_by(StoredFile.id)
    ).all()
    by_id = {row.id: row for row in rows}
    failed_urls = {url for url, row_id in current.items() if row_id in by_id and not by_id[row_id].sha256}
    now = datetime.now(UTC)
    changed = 0
    for row in rows:
        if row.url not in listed:
            keep = False
        elif row.url in current:
            keep = current[row.url] == row.id or (row.url in failed_urls and bool(row.sha256) and bool(row.active))
        else:
            keep = bool(row.active)
        if keep and not row.active:
            row.active = True
            row.removed_at = None
        elif not keep and row.active:
            row.active = False
            row.removed_at = now
            changed += 1
    session.flush()
    return changed


def sanitize_filename(name: str | None) -> str:
    """Reduce an untrusted name to a safe basename (no directories, no control chars)."""
    text = unquote(name or "")
    text = text.replace("\\", "/").split("/")[-1]
    text = re.sub(r"[^A-Za-z0-9._ -]", "_", text).strip(" .")
    if text in {"", ".", ".."}:
        text = "attachment"
    if len(text) > _MAX_NAME:
        stem, dot, ext = text.rpartition(".")
        if dot and 0 < len(ext) <= 10:
            text = stem[: _MAX_NAME - len(ext) - 1] + "." + ext
        else:
            text = text[:_MAX_NAME]
    return text


def filename_from_headers(content_disposition: str | None) -> str | None:
    if not content_disposition:
        return None
    message = Message()
    message["content-disposition"] = content_disposition
    name = message.get_filename()
    return name.strip() if isinstance(name, str) and name.strip() else None


def _url_basename(url: str) -> str:
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1])


def choose_filename(ref: AttachmentRef, content_disposition: str | None, mime_type: str | None) -> str:
    candidates = [filename_from_headers(content_disposition), ref.filename, _url_basename(ref.url)]
    raw = next((c for c in candidates if c and c.strip().lower() not in _GENERIC_NAMES), None)
    name = sanitize_filename(raw)
    if "." not in name and mime_type:
        extension = mimetypes.guess_extension(mime_type)
        if extension:
            name = sanitize_filename(name + extension)
    return name


def _mime_type(content_type: str | None, filename: str) -> str:
    declared = (content_type or "").split(";", 1)[0].strip().lower()
    if declared and declared not in {"application/octet-stream", "binary/octet-stream", "application/download"}:
        return declared
    return guess_mime_type(filename)


def _sam_params(url: str, settings: Settings) -> dict[str, str] | None:
    parts = urlsplit(url)
    if (parts.hostname or "").lower() not in SAM_API_HOSTS:
        return None
    if "api_key" in parse_qs(parts.query):
        return None
    return {"api_key": settings.sam_api_key} if settings.sam_api_key else None


def store_bytes(data: bytes, sha: str, opportunity_id: int, filename: str, settings: Settings) -> Path:
    """Write ``data`` atomically inside the opportunity folder, content-addressed.

    Never overwrites: an existing file with the same name must already hold
    these bytes, otherwise a unique name is used. Goes through the configured
    ``AttachmentStore`` (ADR-065).
    """
    from govcon.enrich.storage import get_store

    return Path(get_store(settings).put(data, sha, opportunity_id, filename))


def write_pages(session: Session, row: StoredFile, extraction: ExtractionResult) -> None:
    """Replace the file's stored pages with ``extraction``'s, and record OCR coverage."""
    session.execute(delete(FilePage).where(FilePage.file_id == row.id))
    row.page_count = extraction.page_count if extraction.page_count is not None else (
        len(extraction.pages) if extraction.pages else None)
    row.ocr_pages = extraction.ocr_pages or None
    row.ocr_failed_pages = extraction.ocr_failed_pages or None
    for page in extraction.pages or []:
        session.add(FilePage(
            file_id=row.id, page_no=page.page_no, label=page.label, text=page.text,
            text_source=page.source, ocr_confidence=page.confidence, char_count=len(page.text),
        ))
    session.flush()


def _existing(session: Session, opportunity_id: int, url: str, sha: str | None) -> StoredFile | None:
    query = select(StoredFile).where(StoredFile.opportunity_id == opportunity_id, StoredFile.url == url)
    query = query.where(StoredFile.sha256 == sha) if sha else query.where(StoredFile.sha256.is_(None))
    return session.scalars(query.order_by(desc(StoredFile.id)).limit(1)).first()


def _record_failure(
    session: Session, opportunity: Opportunity, ref: AttachmentRef, *, snapshot_id: int | None, message: str
) -> StoredFile:
    """Record that the current fetch of ``ref`` failed.

    Always a ``download_failed`` row for this snapshot, even when an earlier
    version of the URL is stored: those bytes were not verified as current, so
    the inventory must block until a fetch succeeds. The earlier version stays
    on record as the last known content.
    """
    row = _existing(session, opportunity.id, ref.url, None)
    if row is None or row.sha256:  # never turn a stored version into a failure row
        row = StoredFile(opportunity_id=opportunity.id, url=ref.url, classification="UNKNOWN", source_origin="government_feed")
        session.add(row)
    row.filename = sanitize_filename(ref.filename or _url_basename(ref.url))
    row.extraction_status = DOWNLOAD_FAILED
    row.extraction_error = message
    row.snapshot_id = snapshot_id
    row.active = True
    row.removed_at = None
    session.flush()
    return row


@dataclass
class FetchedAttachment:
    """One ref's fetch, storage and extraction, done without a database session."""

    ref: AttachmentRef
    error: str | None = None
    sha: str | None = None
    known: bool = False  # these bytes are already stored for this URL
    filename: str | None = None
    mime: str | None = None
    local_path: str | None = None
    extraction: ExtractionResult | None = None


def current_sam_versions(
    session: Session, opportunity_id: int, refs: list[AttachmentRef], snapshot_id: int | None
) -> dict[str, int]:
    """SAM-hosted documents already stored for the notice's current snapshot: ``url -> row id``.

    SAM content changes only with a new notice version (a new snapshot), and
    every SAM API download spends the key's daily quota; re-fetching them on
    each preparation run hit HTTP 429 and blocked the inventory on 2026-10-05.
    These rows are reused instead of fetched again.
    """
    urls = [ref.url for ref in refs if (urlsplit(ref.url).hostname or "").lower() in SAM_API_HOSTS]
    if snapshot_id is None or not urls:
        return {}
    rows = session.execute(
        select(StoredFile.url, StoredFile.id)
        .where(StoredFile.opportunity_id == opportunity_id, StoredFile.url.in_(urls),
               StoredFile.sha256.is_not(None), StoredFile.snapshot_id == snapshot_id)
        .order_by(StoredFile.id)
    )
    return {url: row_id for url, row_id in rows if url is not None}  # the latest stored version wins


def needs_ocr_retry(row: StoredFile, settings: Settings) -> bool:
    """A retained PDF whose unreadable pages may now be read by OCR."""
    is_pdf = row.mime_type == "application/pdf" or (row.filename or "").lower().endswith(".pdf")
    return bool(settings.ocr_enabled and is_pdf and row.sha256 and (
        row.ocr_failed_pages or not (row.extracted_text or "").strip()
    ))


def update_extraction(session: Session, row: StoredFile, extraction: ExtractionResult) -> None:
    """Repair extraction in place; byte identity and classification stay intact."""
    from govcon.workflow.invalidation import lock_one

    # Take the parent lock before flushing caller changes to the file, matching
    # preparation's parent-before-child order. Refresh only after that flush so
    # concurrent repairs see the latest pages without discarding new metadata.
    with session.no_autoflush:
        parent_id = session.scalar(select(Opportunity.id).where(Opportunity.id == row.opportunity_id)
                                   .with_for_update(key_share=True))
    if parent_id is None:
        raise ValueError("Cannot repair a file whose opportunity no longer exists")
    refreshed = lock_one(session, select(StoredFile).where(StoredFile.id == row.id))
    if refreshed is None:
        raise ValueError("Cannot repair a file that no longer exists")
    row = refreshed
    if row.extracted_text and not extraction.text:
        return
    if extraction.pages:
        from govcon.enrich.extract import DEFAULT_LIMITS, PageText, _cap_text

        pages = {page.page_no: page for page in extraction.pages}
        restored = False
        for previous in session.scalars(select(FilePage).where(FilePage.file_id == row.id)):
            if previous.text and previous.text.strip() and not (pages.get(previous.page_no) and pages[previous.page_no].text.strip()):
                pages[previous.page_no] = PageText(
                    previous.page_no, previous.text, previous.text_source,
                    float(previous.ocr_confidence) if previous.ocr_confidence is not None else None, previous.label,
                )
                restored = True
        if restored:
            ordered = [pages[number] for number in sorted(pages)]
            complete = len(ordered) == extraction.page_count and all(page.text.strip() for page in ordered)
            complete = complete and "truncated" not in (extraction.error or "").lower()
            extraction = _cap_text(ExtractionResult(
                "\n\n".join(page.text for page in ordered),
                "success" if complete else extraction.status,
                None if complete else extraction.error,
                page_count=extraction.page_count, pages=ordered,
                ocr_pages=[page.page_no for page in ordered if page.source == "ocr"],
                ocr_failed_pages=[item for item in extraction.ocr_failed_pages
                                  if not isinstance(item.get("page"), int) or not pages.get(item["page"]) or not pages[item["page"]].text.strip()],
            ), DEFAULT_LIMITS)
    row.extracted_text = extraction.text
    row.extraction_status = extraction.status
    row.extraction_error = extraction.error
    write_pages(session, row, extraction)
    session.flush()


def known_versions(
    session: Session, opportunity_id: int, refs: list[AttachmentRef], *,
    settings: Settings | None = None, retry_ocr: bool = True,
) -> dict[str, set[str]]:
    """SHA-256 of every stored version of each listed URL."""
    urls = [ref.url for ref in refs]
    known: dict[str, set[str]] = {url: set() for url in urls}
    settings = settings or get_settings()
    for row in session.scalars(
        select(StoredFile)
        .where(StoredFile.opportunity_id == opportunity_id, StoredFile.url.in_(urls), StoredFile.sha256.is_not(None))
    ):
        if (not retry_ocr or not needs_ocr_retry(row, settings)) and row.url and row.sha256:
            known[row.url].add(row.sha256)
    return known


def fetch_attachment(
    ref: AttachmentRef,
    *,
    opportunity_id: int,
    known_shas: set[str],
    settings: Settings,
    client: httpx.Client,
    resolver: Resolver | None,
) -> FetchedAttachment:
    """Fetch, store and extract one ref (OCR included). Opens no database session."""
    from govcon.enrich.ocr import ocr_config
    from govcon.enrich.source_documents import fetch_dibbs_pdf, sam_description_text

    origin = ref.source_metadata.get("origin")
    max_bytes = int(settings.attachment_max_mb) * 1024 * 1024
    try:
        if origin == DIBBS_RFQ_PDF:
            fetched = fetch_dibbs_pdf(client, ref.url, max_bytes=max_bytes,
                                      interval=settings.dibbs_request_interval_seconds)
        else:
            fetched = safe_fetch(
                client,
                ref.url,
                max_bytes=max_bytes,
                resolver=resolver,
                allow_http=settings.attachment_allow_http,
                params=_sam_params(ref.url, settings),
            )
        data, content_type = fetched.content, fetched.content_type
        if origin == SAM_DESCRIPTION:
            # The notice text, unwrapped from SAM's JSON, is the document.
            data, content_type = sam_description_text(data).encode("utf-8"), "text/plain"
    except FetchError as exc:
        message = redact(f"download failed: {exc}", settings.secret_values())
        logger.warning("attachment download failed for opportunity %s: %s", opportunity_id, message)
        return FetchedAttachment(ref, error=message)

    sha = hashlib.sha256(data).hexdigest()
    if sha in known_shas:
        return FetchedAttachment(ref, sha=sha, known=True)

    guessed = filename_from_headers(fetched.content_disposition) or ref.filename or _url_basename(ref.url)
    mime = _mime_type(content_type, sanitize_filename(guessed))
    filename = choose_filename(ref, fetched.content_disposition, mime)
    try:
        local_path: str | None = str(store_bytes(data, sha, opportunity_id, filename, settings))
    except (OSError, ValueError) as exc:
        logger.warning("failed to save attachment locally: %s", exc)
        local_path = None
    extraction = extract_text(data, mime, filename, ocr=ocr_config(settings))
    return FetchedAttachment(ref, sha=sha, filename=filename, mime=mime, local_path=local_path, extraction=extraction)


def record_fetched(
    session: Session, opportunity: Opportunity, fetched: FetchedAttachment, *, snapshot_id: int | None
) -> StoredFile:
    """Persist one fetch as an attachment version, a refreshed version, or a failure row."""
    ref = fetched.ref
    if fetched.error is not None:
        return _record_failure(session, opportunity, ref, snapshot_id=snapshot_id, message=fetched.error)
    existing = _existing(session, opportunity.id, ref.url, fetched.sha)
    if existing is not None:
        logger.debug("deduplicated attachment for opportunity %s (sha256 match)", opportunity.id)
        # These bytes were just retrieved again: the inventory's freshness check
        # compares source changes with the latest successful download.
        existing.downloaded_at = datetime.now(UTC)
        if fetched.extraction is not None:
            existing.local_path = fetched.local_path or existing.local_path
            update_extraction(session, existing, fetched.extraction)
        session.flush()
        _promote_downloaded(session, existing)
        return existing
    if fetched.known or fetched.extraction is None:
        raise RuntimeError(f"stored version of {ref.url} disappeared while it was being fetched")

    extraction = fetched.extraction
    row = _existing(session, opportunity.id, ref.url, None)  # reuse an earlier failure row
    if row is None:
        row = StoredFile(opportunity_id=opportunity.id, url=ref.url, classification="UNKNOWN", source_origin="government_feed")
        session.add(row)
    row.filename = fetched.filename
    row.local_path = fetched.local_path
    row.mime_type = fetched.mime
    row.sha256 = fetched.sha
    row.extracted_text = extraction.text
    row.extraction_status = extraction.status
    row.extraction_error = extraction.error
    row.snapshot_id = snapshot_id
    row.downloaded_at = datetime.now(UTC)
    row.active = True
    row.removed_at = None
    session.flush()
    write_pages(session, row, extraction)
    _promote_downloaded(session, row)
    return row


def _download_one(
    session: Session,
    opportunity: Opportunity,
    ref: AttachmentRef,
    *,
    settings: Settings,
    client: httpx.Client,
    resolver: Resolver | None,
    snapshot_id: int | None,
) -> StoredFile:
    """Download one ref and persist it as an attachment version (or a failure row)."""
    known = known_versions(session, opportunity.id, [ref], settings=settings)[ref.url]
    fetched = fetch_attachment(ref, opportunity_id=opportunity.id, known_shas=known, settings=settings,
                               client=client, resolver=resolver)
    return record_fetched(session, opportunity, fetched, snapshot_id=snapshot_id)


def process_local_file(
    session: Session,
    opportunity: Opportunity,
    file_path: Path,
    *,
    classification: DataClassification,
    source_origin: str,
    settings: Settings | None = None,
) -> StoredFile:
    """Process a local file: compute SHA-256, extract text, persist.

    Classification and origin are explicit for every local import. Unknown
    legacy metadata is resolved by re-ingest; known classifications never lower.
    """
    if not isinstance(classification, DataClassification):
        raise TypeError("classification must be a DataClassification")
    if classification is DataClassification.UNKNOWN:
        raise ValueError("Local ingest requires a known classification")
    if not source_origin.strip() or len(source_origin) > 200:
        raise ValueError("source_origin must identify the document's source (1–200 characters)")
    from govcon.enrich.ocr import ocr_config

    settings = settings or get_settings()
    data = file_path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    filename = file_path.name
    mime = guess_mime_type(filename)
    extraction = extract_text(data, mime, filename, ocr=ocr_config(settings))

    existing = session.execute(
        select(StoredFile).where(
            StoredFile.opportunity_id == opportunity.id,
            StoredFile.sha256 == sha,
            StoredFile.url.is_(None),
        )
    ).scalars().first()
    if existing is not None:
        if needs_ocr_retry(existing, settings):
            existing.local_path = str(file_path)
            update_extraction(session, existing, extraction)
        existing.source_origin = source_origin.strip()
        existing.local_path = str(store_bytes(data, sha, opportunity.id, filename, settings))
        if classification is DataClassification.PUBLIC:
            _promote_local(session, existing, data)
        else:
            from govcon.enrich.file_class import apply_classification

            apply_classification(
                session, existing, strictest_classification(existing.classification, classification),
                reason="explicit_local_import",
            )
        session.flush()
        return existing

    stored = store_bytes(data, sha, opportunity.id, filename, settings)
    sf = StoredFile(
        opportunity_id=opportunity.id,
        filename=filename,
        classification="UNKNOWN",
        source_origin=source_origin.strip(),
        url=None,
        local_path=str(stored),
        mime_type=mime,
        sha256=sha,
        extracted_text=extraction.text,
        extraction_status=extraction.status,
        extraction_error=extraction.error,
        snapshot_id=latest_snapshot_id(session, opportunity.id),
        downloaded_at=datetime.now(UTC),
        active=True,
    )
    session.add(sf)
    session.flush()
    write_pages(session, sf, extraction)
    if classification is DataClassification.PUBLIC:
        _promote_local(session, sf, data)
    else:
        from govcon.enrich.file_class import apply_classification

        apply_classification(session, sf, classification, reason="explicit_local_import")
    return sf


def _promote_downloaded(session: Session, row: StoredFile) -> None:
    from govcon.enrich.file_class import promote_public_if_verified
    from govcon.enrich.storage import get_store

    data = get_store(get_settings()).read(row.local_path) if row.local_path else None
    promote_public_if_verified(session, row, data)


def _promote_local(session: Session, row: StoredFile, data: bytes) -> None:
    from govcon.enrich.file_class import promote_public_if_verified

    promote_public_if_verified(session, row, data)
