"""SAM award notices and J&A notices are stored, but never as open solicitations."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from govcon.ingest.sam_opportunities import normalize_opportunity
from govcon.matching.eligibility import ELIGIBLE_STATUSES


def _raw(kind: str, *, base: str | None = None, active: str = "Yes") -> dict:
    return {"noticeId": uuid4().hex, "title": "Gloves", "type": kind, "baseType": base or kind, "active": active,
            "postedDate": "2026-10-09", "solicitationNumber": "SPE-1"}


@pytest.mark.parametrize("kind,base,expected", [
    ("Solicitation", None, "open"),
    ("Combined Synopsis/Solicitation", None, "open"),
    ("Sources Sought", None, "open"),
    ("Presolicitation", None, "open"),
    ("Award Notice", None, "awarded"),
    ("Award Notice", "Solicitation", "awarded"),
    ("Justification", None, "notice_only"),
    ("Fair Opportunity / Limited Sources Justification", None, "notice_only"),
    ("Sale of Surplus Property", None, "notice_only"),
    ("Award Notice", "Cancelled", "cancelled"),
])
def test_notice_type_sets_the_status(kind, base, expected):
    assert normalize_opportunity(_raw(kind, base=base)).status == expected


def test_only_open_notices_can_be_matched():
    assert ELIGIBLE_STATUSES == frozenset({"open"})
    assert normalize_opportunity(_raw("Award Notice")).status not in ELIGIBLE_STATUSES


def test_migration_reclassifies_stored_award_notices(upgraded_engine):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from govcon.models import Opportunity

    path = Path(__file__).parents[1] / "alembic/versions/a9b0c1d2e3f4_sam_award_notice_status.py"
    spec = importlib.util.spec_from_file_location("award_status_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    kinds = {"Award Notice": "open", "Justification": "closed", "Solicitation": "open", "Sources Sought": "open"}
    with Session(upgraded_engine) as session:
        rows = {kind: Opportunity(source="sam", source_id=f"st-{uuid4().hex}", title=kind, status=status,
                                  opportunity_type=kind, raw={}, links={}) for kind, status in kinds.items()}
        session.add_all(rows.values())
        session.commit()
        ids = {kind: row.id for kind, row in rows.items()}
    with upgraded_engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
    with upgraded_engine.connect() as conn:
        status = dict(conn.execute(text("SELECT id, status FROM opportunities WHERE id = ANY(:ids)"),
                                   {"ids": list(ids.values())}).all())
    assert status[ids["Award Notice"]] == "awarded"
    assert status[ids["Justification"]] == "notice_only"
    assert status[ids["Solicitation"]] == "open" and status[ids["Sources Sought"]] == "open"
