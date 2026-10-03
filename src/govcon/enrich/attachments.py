"""Attachment download and text extraction.

The downloader accepts only normalised ``AttachmentRef`` values
(``attachment_refs_for``). For each ref it:

- fetches through ``safe_fetch`` (HTTPS, public addresses only on every
  redirect, streamed with a byte cap); the SAM ``api_key`` is added only for
  SAM API hosts and never stored or logged;
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
import os
import re
import tempfile
import uuid
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from govcon.security.classification import DataClassification, strictest_classification
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from govcon.config import Settings, get_settings
from govcon.enrich.attachment_refs import AttachmentRef, attachment_refs_for
from govcon.enrich.extract import extract_text, guess_mime_type
from govcon.enrich.safe_fetch import FetchError, Resolver, safe_fetch
from govcon.http import build_client
from govcon.logging import redact
from govcon.ingest.snapshots import current_snapshot_id
from govcon.models import Opportunity, StoredFile

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
    results: list[StoredFile] = []
    if refs:
        own_client = client is None
        client = client or build_client(settings, timeout=60.0)
        try:
            for ref in refs:
                results.append(
                    _download_one(
                        session,
                        opportunity,
                        ref,
                        settings=settings,
                        client=client,
                        resolver=resolver,
                        snapshot_id=snapshot_id,
                    )
                )
        finally:
            if own_client:
                client.close()
    else:
        logger.info("opportunity %s lists no attachments", opportunity.id)
    current = {ref.url: row.id for ref, row in zip(refs, results)}
    reconcile_attachment_versions(session, opportunity, refs, current=current)
    return results


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
    these bytes, otherwise a unique name is used.
    """
    base = (Path(settings.data_dir) / "attachments" / str(int(opportunity_id))).resolve()
    base.mkdir(parents=True, exist_ok=True)
    safe = sanitize_filename(filename)
    target = (base / f"{sha[:16]}_{safe}").resolve()
    if target.parent != base:
        raise ValueError("attachment path escapes the opportunity folder")
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() == sha:
            return target
        target = (base / f"{sha[:16]}_{uuid.uuid4().hex[:8]}_{safe}").resolve()
    fd, tmp_name = tempfile.mkstemp(dir=base, prefix=".part-")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, target)  # fails instead of replacing an existing file
        except FileExistsError:
            if hashlib.sha256(target.read_bytes()).hexdigest() != sha:
                raise
        except OSError:
            if target.exists():
                raise
            os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


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
        row = StoredFile(opportunity_id=opportunity.id, url=ref.url, classification="PUBLIC", source_origin="government_feed")
        session.add(row)
    row.filename = sanitize_filename(ref.filename or _url_basename(ref.url))
    row.extraction_status = DOWNLOAD_FAILED
    row.extraction_error = message
    row.snapshot_id = snapshot_id
    row.active = True
    row.removed_at = None
    session.flush()
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
    try:
        fetched = safe_fetch(
            client,
            ref.url,
            max_bytes=int(settings.attachment_max_mb) * 1024 * 1024,
            resolver=resolver,
            allow_http=settings.attachment_allow_http,
            params=_sam_params(ref.url, settings),
        )
    except FetchError as exc:
        message = redact(f"download failed: {exc}", settings.secret_values())
        logger.warning("attachment download failed for opportunity %s: %s", opportunity.id, message)
        return _record_failure(session, opportunity, ref, snapshot_id=snapshot_id, message=message)

    data = fetched.content
    sha = hashlib.sha256(data).hexdigest()
    existing = _existing(session, opportunity.id, ref.url, sha)
    if existing is not None:
        logger.debug("deduplicated attachment for opportunity %s (sha256 match)", opportunity.id)
        # These bytes were just retrieved again: the inventory's freshness check
        # compares source changes with the latest successful download.
        existing.downloaded_at = datetime.now(UTC)
        session.flush()
        return existing

    guessed = filename_from_headers(fetched.content_disposition) or ref.filename or _url_basename(ref.url)
    mime = _mime_type(fetched.content_type, sanitize_filename(guessed))
    filename = choose_filename(ref, fetched.content_disposition, mime)
    try:
        local_path: Path | None = store_bytes(data, sha, opportunity.id, filename, settings)
    except (OSError, ValueError) as exc:
        logger.warning("failed to save attachment locally: %s", exc)
        local_path = None
    extraction = extract_text(data, mime, filename)

    row = _existing(session, opportunity.id, ref.url, None)  # reuse an earlier failure row
    if row is None:
        row = StoredFile(opportunity_id=opportunity.id, url=ref.url, classification="PUBLIC", source_origin="government_feed")
        session.add(row)
    row.filename = filename
    row.local_path = str(local_path) if local_path else None
    row.mime_type = mime
    row.sha256 = sha
    row.extracted_text = extraction.text
    row.extraction_status = extraction.status
    row.extraction_error = extraction.error
    row.snapshot_id = snapshot_id
    row.downloaded_at = datetime.now(UTC)
    row.active = True
    row.removed_at = None
    session.flush()
    return row


def process_local_file(
    session: Session,
    opportunity: Opportunity,
    file_path: Path,
    *,
    classification: DataClassification,
    source_origin: str,
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
    data = file_path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    filename = file_path.name
    mime = guess_mime_type(filename)
    extraction = extract_text(data, mime, filename)

    existing = session.execute(
        select(StoredFile).where(
            StoredFile.opportunity_id == opportunity.id,
            StoredFile.sha256 == sha,
            StoredFile.url.is_(None),
        )
    ).scalars().first()
    if existing is not None:
        if existing.classification == DataClassification.UNKNOWN.value:
            # Re-ingest is the explicit classification step for legacy files.
            existing.classification = classification.value
            existing.source_origin = source_origin.strip()
        else:
            existing.classification = strictest_classification(existing.classification, classification).value
        session.flush()
        return existing

    sf = StoredFile(
        opportunity_id=opportunity.id,
        filename=filename,
        classification=classification.value,
        source_origin=source_origin.strip(),
        url=None,
        local_path=str(file_path),
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
    return sf
