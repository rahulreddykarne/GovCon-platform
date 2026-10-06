"""In-app bot run history and the human approvals queue.

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-10-06
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d6e7f8a9b0c1"
down_revision = "c5d6e7f8a9b0"
branch_labels = None
depends_on = None

_BOT_NAMES = (
    "'orchestrator','discovery','document','matching','bid_decision',"
    "'compliance','amendment','awards','alert','operations'"
)
_BOT_STATUSES = (
    "'queued','running','succeeded','completed_with_errors','failed',"
    "'skipped','blocked','waiting_approval'"
)
_OLD_TASKS = (
    "('proposal_generation','ai_analysis','solicitation_summary','scheduler_chain','opportunity_preparation',"
    "'notification_email','quote_extraction')"
)
_NEW_TASKS = (
    "('proposal_generation','ai_analysis','solicitation_summary','scheduler_chain','opportunity_preparation',"
    "'notification_email','quote_extraction','bot_run')"
)


def upgrade() -> None:
    op.create_table(
        "bot_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("bot_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="running"),
        sa.Column("trigger", sa.Text(), nullable=False, server_default="manual"),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("opportunity_id", sa.BigInteger(), sa.ForeignKey("opportunities.id", ondelete="CASCADE")),
        sa.Column("source_revision", sa.Text()),
        sa.Column("parent_run_id", sa.BigInteger(), sa.ForeignKey("bot_runs.id", ondelete="SET NULL")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("inputs", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("outputs", postgresql.JSONB()),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("idempotency_key", name="uq_bot_runs_idempotency_key"),
        sa.CheckConstraint(f"bot_name IN ({_BOT_NAMES})", name="ck_bot_runs_bot_name"),
        sa.CheckConstraint(f"status IN ({_BOT_STATUSES})", name="ck_bot_runs_status"),
    )
    op.create_index("ix_bot_runs_bot_started", "bot_runs", ["bot_name", "started_at"])
    op.create_index("ix_bot_runs_opportunity", "bot_runs", ["opportunity_id", "started_at"])
    op.create_table(
        "bot_approvals",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("bot_run_id", sa.BigInteger(), sa.ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("opportunity_id", sa.BigInteger(), sa.ForeignKey("opportunities.id", ondelete="CASCADE")),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decided_by_user_id", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("decision_note", sa.Text()),
        sa.CheckConstraint("status IN ('pending','approved','rejected')", name="ck_bot_approvals_status"),
    )
    op.create_index("ix_bot_approvals_status", "bot_approvals", ["status", "requested_at"])
    op.drop_constraint("ck_tasks_task_type", "tasks")
    op.create_check_constraint("ck_tasks_task_type", "tasks", f"task_type IN {_NEW_TASKS}")


def downgrade() -> None:
    op.drop_constraint("ck_tasks_task_type", "tasks")
    op.create_check_constraint("ck_tasks_task_type", "tasks", f"task_type IN {_OLD_TASKS}")
    op.drop_index("ix_bot_approvals_status", table_name="bot_approvals")
    op.drop_table("bot_approvals")
    op.drop_index("ix_bot_runs_opportunity", table_name="bot_runs")
    op.drop_index("ix_bot_runs_bot_started", table_name="bot_runs")
    op.drop_table("bot_runs")
