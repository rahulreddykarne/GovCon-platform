"""Per-opportunity lock so two analysis or compliance runs cannot race.

A dedicated connection holds a session-level advisory lock for the whole
run (including the period with no business transaction open during an AI
call). The second runner is rejected with :class:`AnalysisInProgress`; the
worker retries after a short wait.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from govcon.tasks.errors import TaskBlocked

ANALYSIS_LOCK_NS = 742903
ANALYSIS_LOCK_WAIT_SECONDS = 60


class AnalysisInProgress(RuntimeError):
    """Another analysis or compliance run already holds this opportunity."""

    def __init__(self, opportunity_id: int) -> None:
        self.opportunity_id = opportunity_id
        super().__init__(
            f"Analysis or compliance is already running for opportunity {opportunity_id}. "
            "Wait for it to finish, then retry."
        )


def _bind(session: Session) -> Engine | Connection:
    bind = session.get_bind()
    return getattr(bind, "engine", bind)


@contextmanager
def hold_analysis_lock(session: Session, opportunity_id: int) -> Iterator[None]:
    """Hold the opportunity's analysis lock until the block exits.

    Uses ``pg_try_advisory_lock`` on a dedicated connection so the lock
    survives the recorded-pass pattern (commit, call the provider, reopen).
    """
    engine = _bind(session)
    if getattr(engine.dialect, "name", "") != "postgresql":
        yield
        return
    with engine.connect() as lock_conn:
        got = lock_conn.execute(
            text("SELECT pg_try_advisory_lock(:ns, :opp)"),
            {"ns": ANALYSIS_LOCK_NS, "opp": int(opportunity_id)},
        ).scalar()
        if not got:
            raise AnalysisInProgress(opportunity_id)
        try:
            yield
        finally:
            lock_conn.execute(
                text("SELECT pg_advisory_unlock(:ns, :opp)"),
                {"ns": ANALYSIS_LOCK_NS, "opp": int(opportunity_id)},
            )


def analysis_in_progress_blocked(exc: AnalysisInProgress) -> TaskBlocked:
    """Park the colliding task so a person sees a clear wait, then retry."""
    from datetime import UTC, datetime, timedelta

    return TaskBlocked(
        str(exc),
        status="waiting_for_input",
        owner_role="owner",
        next_action=(
            "Another analysis or compliance run is in progress for this opportunity. "
            "Wait for it to finish (about a minute), then retry. Do not start a second run."
        ),
        resume_at=datetime.now(UTC) + timedelta(seconds=ANALYSIS_LOCK_WAIT_SECONDS),
    )
