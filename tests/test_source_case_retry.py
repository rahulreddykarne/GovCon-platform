"""Sanitized SAM and DIBBS documents: a failed download is retried on the next run.

No test in this module contacts SAM or DIBBS.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select

from govcon.config import Settings
from govcon.enrich.attachment_refs import attachment_refs_for
from govcon.models import IngestionRun, Opportunity, StoredFile
from govcon.scheduler.jobs import step_source_documents


def test_failed_source_documents_are_retried(db, monkeypatch) -> None:
    now = datetime.now(UTC)
    older = list(db.scalars(select(IngestionRun)).all())
    saved = [(row.id, row.started_at) for row in older]
    for row in older:
        row.started_at = now - timedelta(days=2)
    db.add(IngestionRun(job="sam_opportunities", started_at=now - timedelta(minutes=1), status="succeeded"))
    db.add(IngestionRun(job="dibbs_index", started_at=now - timedelta(minutes=1), status="succeeded"))
    sam = Opportunity(
        source="sam", source_id=f"SAN-{uuid4().hex[:8]}", title="Sanitized SAM notice",
        status="open", solicitation_number="SPE4A726T0001",
        links={"description": "https://api.sam.gov/prod/opportunities/v2/noticedesc?noticeid=sanitized"},
        raw={"demo": True},
    )
    dibbs = Opportunity(
        source="dibbs", source_id=f"SAN-{uuid4().hex[:8]}", title="Sanitized DIBBS RFQ",
        status="open", solicitation_number="SPE4A726T0001", raw={"demo": True}, links={},
    )
    db.add_all([sam, dibbs])
    db.commit()
    calls: dict[int, int] = {}

    def fake_download(session, opportunity, settings=None):
        calls[opportunity.id] = calls.get(opportunity.id, 0) + 1
        if calls[opportunity.id] == 1:
            raise RuntimeError("sanitized transport failed")
        for ref in attachment_refs_for(opportunity):
            session.add(StoredFile(
                opportunity_id=opportunity.id, url=ref.url, sha256=uuid4().hex, active=True,
                classification="PUBLIC", source_origin="sanitized_case", extraction_status="success",
            ))
        session.flush()

    monkeypatch.setattr("govcon.enrich.attachments.download_attachments", fake_download)
    settings = Settings(_env_file=None)
    try:
        first = step_source_documents(db, settings)
        assert first.status == "completed_with_errors"
        assert first.fetched == 0
        assert "sanitized transport failed" in (first.error or "")
        second = step_source_documents(db, settings)
        assert second.status == "succeeded"
        assert second.fetched == 2
        assert calls[sam.id] == 2 and calls[dibbs.id] == 2
    finally:
        for row_id, started in saved:
            row = db.get(IngestionRun, row_id)
            if row is not None:
                row.started_at = started
        db.commit()
