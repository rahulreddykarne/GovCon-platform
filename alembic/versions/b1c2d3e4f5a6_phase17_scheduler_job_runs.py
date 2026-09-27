"""Phase 17 — Scheduler job runs tracking table.

Adds ``scheduler_job_runs`` to persist chain-level execution history for the
APScheduler job chains.  Individual step results continue to be written to the
existing ``ingestion_runs`` table.

Revision ID: b1c2d3e4f5a6
Revises: f2a3b4c5d6e7
Create Date: 2026-09-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, TEXT

# revision identifiers, used by Alembic.
revision = "b1c2d3e4f5a6"
down_revision = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scheduler_job_runs",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("chain_name", sa.Text, nullable=False),
        sa.Column("trigger", sa.Text, nullable=False, server_default="manual"),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default="running"),
        sa.Column("steps_completed", JSONB, nullable=True),
        sa.Column("failed_step", sa.Text, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("row_counts", JSONB, nullable=True),
    )
    op.create_index(
        "ix_scheduler_job_runs_chain_started",
        "scheduler_job_runs",
        ["chain_name", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_scheduler_job_runs_chain_started", table_name="scheduler_job_runs")
    op.drop_table("scheduler_job_runs")
