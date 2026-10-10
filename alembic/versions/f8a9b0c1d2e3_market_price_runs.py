"""Web market price research for pursued product opportunities.

Idempotent: a database where this revision id was used for the AI provider
usage migration (an earlier branch) can be stamped here without the table;
``b0c1d2e3f4a5`` repairs it by running the same steps.

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-10-10
"""
from alembic import op

from govcon.migration_helpers import constraint_definition, ensure_market_price_runs, has_index, has_table

revision = "f8a9b0c1d2e3"
down_revision = "e7f8a9b0c1d2"
branch_labels = None
depends_on = None

_OLD_TASKS = (
    "('proposal_generation','ai_analysis','solicitation_summary','scheduler_chain','opportunity_preparation',"
    "'notification_email','quote_extraction','bot_run')"
)


def upgrade() -> None:
    ensure_market_price_runs()


def downgrade() -> None:
    op.execute("DELETE FROM tasks WHERE task_type = 'market_price_research'")
    if constraint_definition("tasks", "ck_tasks_task_type") is not None:
        op.drop_constraint("ck_tasks_task_type", "tasks")
    op.create_check_constraint("ck_tasks_task_type", "tasks", f"task_type IN {_OLD_TASKS}")
    if has_index("market_price_runs", "ix_market_price_runs_opportunity_created"):
        op.drop_index("ix_market_price_runs_opportunity_created", table_name="market_price_runs")
    if has_table("market_price_runs"):
        op.drop_table("market_price_runs")
