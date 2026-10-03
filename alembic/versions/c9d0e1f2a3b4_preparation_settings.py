"""Opportunity preparation tasks and owner-editable workflow settings (ADR-067).

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-10-03
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None

_OLD = "('proposal_generation','ai_analysis','solicitation_summary','scheduler_chain')"
_NEW = "('proposal_generation','ai_analysis','solicitation_summary','scheduler_chain','opportunity_preparation')"


def upgrade():
    op.drop_constraint("ck_tasks_task_type", "tasks")
    op.create_check_constraint("ck_tasks_task_type", "tasks", f"task_type IN {_NEW}")
    op.create_table(
        "app_settings",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("updated_by_user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.execute(
        """
        CREATE TRIGGER trg_app_settings_set_updated_at
        BEFORE UPDATE ON app_settings
        FOR EACH ROW
        EXECUTE FUNCTION govcon_set_updated_at()
        """
    )


def downgrade():
    op.execute("DROP TRIGGER IF EXISTS trg_app_settings_set_updated_at ON app_settings")
    op.drop_table("app_settings")
    op.execute("DELETE FROM tasks WHERE task_type = 'opportunity_preparation'")
    op.drop_constraint("ck_tasks_task_type", "tasks")
    op.create_check_constraint("ck_tasks_task_type", "tasks", f"task_type IN {_OLD}")
