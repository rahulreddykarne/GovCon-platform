"""Match reconciliation and stored NSN candidates.

``matches.active`` marks matches the current watchlist criteria and the
opportunity's current state still produce. A match that stops matching (the
criteria changed, the opportunity closed, or the watchlist was disabled) is
marked inactive with ``deactivated_at`` and ``inactive_reason`` instead of being
deleted, so its triage status and alert history stay auditable.

``opportunities.nsn_candidates`` stores every NSN parsed from the source, not
only the first one, so watchlist NSN matching can check all of them. Existing
rows are backfilled from the snapshot of their current content, falling back
to the single ``nsn`` column.

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-10-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e9f0a1b2c3d4"
down_revision = "d8e9f0a1b2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("matches", sa.Column("active", sa.Boolean, nullable=False, server_default=sa.text("true")))
    op.add_column("matches", sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("matches", sa.Column("inactive_reason", sa.Text, nullable=True))
    op.create_index("ix_matches_watchlist_active", "matches", ["watchlist_id", "active"])

    op.add_column("opportunities", sa.Column("nsn_candidates", postgresql.ARRAY(sa.Text), nullable=True))
    op.execute(
        """
        UPDATE opportunities AS o
        SET nsn_candidates = ARRAY(SELECT jsonb_array_elements_text(s.normalized -> 'nsn_candidates'))
        FROM opportunity_snapshots AS s
        WHERE s.opportunity_id = o.id
          AND s.content_hash = o.raw_hash
          AND jsonb_typeof(s.normalized -> 'nsn_candidates') = 'array'
          AND jsonb_array_length(s.normalized -> 'nsn_candidates') > 0
        """
    )
    op.execute(
        """
        UPDATE opportunities
        SET nsn_candidates = ARRAY[nsn]
        WHERE nsn_candidates IS NULL AND nsn IS NOT NULL AND nsn <> ''
        """
    )


def downgrade() -> None:
    op.drop_column("opportunities", "nsn_candidates")
    op.drop_index("ix_matches_watchlist_active", table_name="matches")
    op.drop_column("matches", "inactive_reason")
    op.drop_column("matches", "deactivated_at")
    op.drop_column("matches", "active")
