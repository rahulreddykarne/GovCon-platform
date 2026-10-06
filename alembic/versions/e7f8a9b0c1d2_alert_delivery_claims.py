"""Digest delivery claims so a crash after SMTP cannot send the same digest twice.

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-10-06
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e7f8a9b0c1d2"
down_revision = "d6e7f8a9b0c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alert_deliveries",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("claim_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="sending"),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("match_ids", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("claim_key", name="uq_alert_deliveries_claim_key"),
        sa.CheckConstraint("status IN ('sending', 'sent', 'failed')", name="ck_alert_deliveries_status"),
    )


def downgrade() -> None:
    op.drop_table("alert_deliveries")
