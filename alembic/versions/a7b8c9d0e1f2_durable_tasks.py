"""Durable background tasks (ADR-061).

Revision ID: a7b8c9d0e1f2
Revises: 7f4d81a9c630
Create Date: 2026-10-03
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "a7b8c9d0e1f2"
down_revision = "7f4d81a9c630"
branch_labels = None
depends_on = None

_TASK_TYPES = "('proposal_generation','ai_analysis','solicitation_summary','scheduler_chain')"
_STATUSES = (
    "('queued','running','waiting_for_input','waiting_for_budget','retrying',"
    "'succeeded','failed','cancelled')"
)


def upgrade():
    op.create_table(
        "tasks",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("task_type", sa.Text(), nullable=False),
        sa.Column("opportunity_id", sa.BigInteger(), sa.ForeignKey("opportunities.id", ondelete="CASCADE")),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("dedup_key", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("input_revision", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("checkpoint", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("current_step", sa.Text()),
        sa.Column("blocker_owner_role", sa.Text()),
        sa.Column("blocker_owner_user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("blocker_next_action", sa.Text()),
        sa.Column("lease_owner", sa.Text()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claim_token", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_error_type", sa.Text()),
        sa.Column("last_error", sa.Text()),
        sa.Column("superseded_by_task_id", sa.BigInteger(), sa.ForeignKey("tasks.id")),
        sa.Column(
            "scheduler_job_run_id", sa.BigInteger(),
            sa.ForeignKey("scheduler_job_runs.id", ondelete="SET NULL"),
        ),
        sa.Column("created_by_user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(f"task_type IN {_TASK_TYPES}", name="ck_tasks_task_type"),
        sa.CheckConstraint(f"status IN {_STATUSES}", name="ck_tasks_status"),
        sa.CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="ck_tasks_attempts"),
    )
    op.create_index(
        "uq_tasks_dedup_key_active", "tasks", ["dedup_key"], unique=True,
        postgresql_where=sa.text("status NOT IN ('succeeded','failed','cancelled')"),
    )
    op.create_index("ix_tasks_status_next_attempt_at", "tasks", ["status", "next_attempt_at"])
    op.create_index("ix_tasks_opportunity_type_created", "tasks", ["opportunity_id", "task_type", "created_at"])
    op.execute(
        """
        CREATE TRIGGER trg_tasks_set_updated_at
        BEFORE UPDATE ON tasks
        FOR EACH ROW
        EXECUTE FUNCTION govcon_set_updated_at()
        """
    )


def downgrade():
    op.execute("DROP TRIGGER IF EXISTS trg_tasks_set_updated_at ON tasks")
    op.drop_table("tasks")
