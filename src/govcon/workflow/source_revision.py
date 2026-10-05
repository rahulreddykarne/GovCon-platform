"""Source revision stamps for derived artifacts.

A derived artifact (solicitation analysis, decision package, proposal
version, submission pre-flight) records the source revision it was built
from. Consumers refuse an artifact whose stamp no longer matches the current
revision. Artifacts created before stamping existed carry no stamp and are
treated as unknown rather than stale.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Self

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.models import Opportunity, StoredFile

SOURCE_REVISION_KEY = "source_revision"
REVISION_PREFIX = "v2:"


class SourceRevision(str):
    """A ``v2:`` revision stamp that also knows the pre-v2 stamp of the same source.

    Stored stamps are plain strings. ``is_stale`` compares a stamp written
    before v2 with ``legacy`` so upgrading does not make every existing
    artifact look stale (or a stale one look current).
    """

    legacy: str

    def __new__(cls, value: str, legacy: str) -> Self:
        obj = super().__new__(cls, value)
        obj.legacy = legacy
        return obj


def current_source_revision(session: Session, opportunity_id: int) -> str | None:
    """Revision of the source an artifact is built from.

    The source payload plus the current attachment set: each active file's
    identity (URL, or file id for a local file), content hash and whether its
    latest fetch failed. Removed or replaced versions are history and do not
    count, so adding, removing, replacing or reverting an attachment changes
    the revision.
    """
    opportunity = session.get(Opportunity, opportunity_id)
    if opportunity is None:
        return None
    rows = session.execute(
        select(StoredFile.id, StoredFile.url, StoredFile.sha256, StoredFile.extraction_status, StoredFile.active)
        .where(StoredFile.opportunity_id == opportunity_id)
    ).all()
    current = sorted(
        f"{url or f'file:{file_id}'}|{sha or ''}|{'failed' if status == 'download_failed' else 'ok'}"
        for file_id, url, sha, status, active in rows
        if active
    )
    basis: dict[str, object] = {
        "opportunity_id": opportunity_id,
        "raw_hash": opportunity.raw_hash,
        "attachments": current,
    }
    policy = sorted((row.id, row.classification, row.source_origin) for row in session.scalars(
        select(StoredFile).where(StoredFile.opportunity_id == opportunity_id)
    ) if row.classification != "PUBLIC")
    if policy:
        basis["document_policy"] = policy
    value = REVISION_PREFIX + hashlib.sha256(json.dumps(basis, sort_keys=True).encode("utf-8")).hexdigest()
    legacy_basis: dict[str, object] = {
        "opportunity_id": opportunity_id,
        "raw_hash": opportunity.raw_hash,
        "files": sorted(sha or "" for _, _, sha, _, _ in rows),
    }
    if policy:
        legacy_basis["document_policy"] = policy
    legacy = hashlib.sha256(json.dumps(legacy_basis, sort_keys=True).encode("utf-8")).hexdigest()
    return SourceRevision(value, legacy)


def stamp_of(container: Any) -> str | None:
    """Read the revision stamp from a JSON dict (manifest, metadata, run output)."""
    if isinstance(container, dict):
        value = container.get(SOURCE_REVISION_KEY)
        return value if isinstance(value, str) else None
    return None


def is_stale(stamp: str | None, current: str | None) -> bool:
    """True only when both stamps are known and differ.

    A stamp written before v2 is compared with the current source's pre-v2
    stamp when ``current`` carries it.
    """
    if stamp is None or current is None:
        return False
    if not stamp.startswith(REVISION_PREFIX) and isinstance(current, SourceRevision):
        return stamp != current.legacy
    return stamp != str(current)
