"""Web market price research for pursued product opportunities.

Idempotent: a database another branch stamped at this revision id (with its
AI usage tables and no ``market_price_runs``) is repaired by the next
revision, and running this upgrade over an existing table is a no-op.

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-10-10
"""
from alembic import op

from govcon.migration_helpers import (
    MARKET_PRICE_TASK_TYPES_BEFORE,
    constraint_definition,
    ensure_market_price_runs_v1,
    index_exists,
    table_exists,
)

revision = "f8a9b0c1d2e3"
down_revision = "e7f8a9b0c1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ensure_market_price_runs_v1()


def downgrade() -> None:
    op.execute("DELETE FROM tasks WHERE task_type = 'market_price_research'")
    if constraint_definition("tasks", "ck_tasks_task_type") is not None:
        op.drop_constraint("ck_tasks_task_type", "tasks")
    op.create_check_constraint("ck_tasks_task_type", "tasks", f"task_type IN {MARKET_PRICE_TASK_TYPES_BEFORE}")
    if table_exists("market_price_runs"):
        if index_exists("market_price_runs", "ix_market_price_runs_opportunity_created"):
            op.drop_index("ix_market_price_runs_opportunity_created", table_name="market_price_runs")
        op.drop_table("market_price_runs")
