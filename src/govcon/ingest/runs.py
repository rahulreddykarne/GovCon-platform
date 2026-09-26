"""Ingestion run bookkeeping for jobs that write ``ingestion_runs``."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from govcon.models import IngestionRun


@dataclass
class IngestStats:
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: list[str] = field(default_factory=list)

    def add(self, other: IngestStats) -> None:
        self.fetched += other.fetched
        self.inserted += other.inserted
        self.updated += other.updated
        self.unchanged += other.unchanged
        self.errors.extend(other.errors)


def start_run(session: Session, job: str) -> IngestionRun:
    run = IngestionRun(
        job=job,
        started_at=datetime.now(timezone.utc),
        status="running",
        fetched=0,
        inserted=0,
        updated=0,
        unchanged=0,
        errors={"messages": []},
    )
    session.add(run)
    session.flush()
    return run


def finish_run(run: IngestionRun, stats: IngestStats, *, status: str) -> None:
    run.finished_at = datetime.now(timezone.utc)
    run.status = status
    run.fetched = stats.fetched
    run.inserted = stats.inserted
    run.updated = stats.updated
    run.unchanged = stats.unchanged
    run.errors = {"messages": list(stats.errors)}
