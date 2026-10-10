"""SAM award notices, justifications and surplus sales are not open solicitations.

Ingest used to store every active SAM notice as ``open``, so award notices and
J&A notices appeared beside biddable solicitations and could be matched.
They now get ``awarded`` / ``notice_only``; this reclassifies stored rows.

Idempotent: only ``open`` / ``closed`` rows are touched, so a second run (or
a database that already has the new statuses) changes nothing. It creates no
schema objects.

Revision ID: a9b0c1d2e3f4
Revises: f8a9b0c1d2e3
Create Date: 2026-10-10
"""
from alembic import op

from govcon.migration_helpers import has_column

revision = "a9b0c1d2e3f4"
down_revision = "f8a9b0c1d2e3"
branch_labels = None
depends_on = None

_KIND = "lower(coalesce(opportunity_type, '') || ' ' || coalesce(raw->>'baseType', ''))"


def upgrade() -> None:
    if not has_column("opportunities", "status"):
        return
    op.execute(f"""
        UPDATE opportunities SET status = 'awarded', updated_at = now()
        WHERE source = 'sam' AND status IN ('open', 'closed') AND {_KIND} LIKE '%award%'
          AND {_KIND} NOT LIKE '%cancel%'
    """)
    op.execute(f"""
        UPDATE opportunities SET status = 'notice_only', updated_at = now()
        WHERE source = 'sam' AND status IN ('open', 'closed')
          AND ({_KIND} LIKE '%justification%' OR {_KIND} LIKE '%sale of surplus%')
          AND {_KIND} NOT LIKE '%cancel%'
    """)


def downgrade() -> None:
    op.execute("UPDATE opportunities SET status = 'open' WHERE source = 'sam' AND status IN ('awarded', 'notice_only')")
