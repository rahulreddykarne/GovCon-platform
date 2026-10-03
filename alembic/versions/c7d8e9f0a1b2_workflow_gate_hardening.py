"""Workflow gate hardening — pin the approved proposal version.

Adds ``proposals.approved_version_id`` so final approval applies to one exact
immutable proposal version. Creating a new version after approval clears it
and returns the proposal to ``draft``.

Revision ID: c7d8e9f0a1b2
Revises: b1c2d3e4f5a6
Create Date: 2026-10-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c7d8e9f0a1b2"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "proposals",
        sa.Column(
            "approved_version_id",
            sa.BigInteger,
            sa.ForeignKey("proposal_versions.id", name="fk_proposals_approved_version_id"),
            nullable=True,
        ),
    )
    # Existing final approvals applied to whatever was current at the time.
    op.execute(
        "UPDATE proposals SET approved_version_id = current_version_id "
        "WHERE status = 'final_approved' AND current_version_id IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_constraint("fk_proposals_approved_version_id", "proposals", type_="foreignkey")
    op.drop_column("proposals", "approved_version_id")
