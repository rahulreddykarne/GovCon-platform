"""Attachment versions — current-snapshot membership for stored files.

Each ``files`` row is an immutable attachment version (one URL + SHA-256,
linked to the snapshot it was downloaded for). ``active`` marks the versions
the current source still lists; a removed or replaced attachment is marked
inactive with ``removed_at`` instead of being deleted, so history stays
auditable and the compliance inventory reads only current files.

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-10-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d8e9f0a1b2c3"
down_revision = "c7d8e9f0a1b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("files", sa.Column("active", sa.Boolean, nullable=False, server_default=sa.text("true")))
    op.add_column("files", sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_files_opportunity_active", "files", ["opportunity_id", "active"])


def downgrade() -> None:
    op.drop_index("ix_files_opportunity_active", table_name="files")
    op.drop_column("files", "removed_at")
    op.drop_column("files", "active")
