"""Web market price research for pursued product opportunities.

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-10-10
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f8a9b0c1d2e3"
down_revision = "e7f8a9b0c1d2"
branch_labels = None
depends_on = None

_OLD_TASKS = (
    "('proposal_generation','ai_analysis','solicitation_summary','scheduler_chain','opportunity_preparation',"
    "'notification_email','quote_extraction','bot_run')"
)
_NEW_TASKS = (
    "('proposal_generation','ai_analysis','solicitation_summary','scheduler_chain','opportunity_preparation',"
    "'notification_email','quote_extraction','bot_run','market_price_research')"
)


def upgrade() -> None:
    op.create_table(
        "market_price_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("opportunity_id", sa.BigInteger(), sa.ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.BigInteger(), sa.ForeignKey("tasks.id", ondelete="SET NULL")),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("source_revision", sa.Text()),
        sa.Column("product", postgresql.JSONB()),
        sa.Column("provider", sa.Text()),
        sa.Column("model", sa.Text()),
        sa.Column("prompt_version", sa.Text()),
        sa.Column("prompt_sha256", sa.Text()),
        sa.Column("listings", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("excluded", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("estimate_unit_cost", sa.Numeric()),
        sa.Column("estimate_low", sa.Numeric()),
        sa.Column("estimate_high", sa.Numeric()),
        sa.Column("estimate_confidence", sa.Text()),
        sa.Column("estimate_basis", sa.Text()),
        sa.Column("quantity", sa.Numeric()),
        sa.Column("unit", sa.Text()),
        sa.Column("estimated_total_cost", sa.Numeric()),
        sa.Column("usage", postgresql.JSONB()),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("note", sa.Text()),
        sa.CheckConstraint("status IN ('completed','no_results','skipped','blocked','failed')",
                           name="ck_market_price_runs_status"),
    )
    op.create_index("ix_market_price_runs_opportunity_created", "market_price_runs", ["opportunity_id", "created_at"])
    op.drop_constraint("ck_tasks_task_type", "tasks")
    op.create_check_constraint("ck_tasks_task_type", "tasks", f"task_type IN {_NEW_TASKS}")


def downgrade() -> None:
    op.execute("DELETE FROM tasks WHERE task_type = 'market_price_research'")
    op.drop_constraint("ck_tasks_task_type", "tasks")
    op.create_check_constraint("ck_tasks_task_type", "tasks", f"task_type IN {_OLD_TASKS}")
    op.drop_index("ix_market_price_runs_opportunity_created", table_name="market_price_runs")
    op.drop_table("market_price_runs")
