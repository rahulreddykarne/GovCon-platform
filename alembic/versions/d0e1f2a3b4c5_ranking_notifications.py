"""Recommendations, explainable ranking, email delivery and review escalation (ADR-068..070).

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-10-03
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "d0e1f2a3b4c5"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None

_OLD_TYPES = (
    "'review_assigned', 'reviewer_completed', 'new_reviewer_comment', "
    "'ai_flagged_comment_needs_evidence', 'review_quorum_satisfied', "
    "'second_review_required', 'second_review_requested', 'review_reassigned', "
    "'approval_pending', 'material_amendment_after_review', "
    "'proposal_package_generated', 'submission_ready'"
)
# Stage 3 adds reminders, escalations and auto-pursue; stages 4 and 5 add
# registration expiry and outcome suggestions, so the constraint changes once.
_NEW_TYPES = _OLD_TYPES + (
    ", 'review_reminder', 'review_overdue_escalation', 'deadline_escalation', 'auto_pursued', "
    "'registration_expiring', 'outcome_suggested'"
)
_OLD_TASKS = "('proposal_generation','ai_analysis','solicitation_summary','scheduler_chain','opportunity_preparation')"
_NEW_TASKS = (
    "('proposal_generation','ai_analysis','solicitation_summary','scheduler_chain','opportunity_preparation',"
    "'notification_email')"
)


def upgrade():
    op.create_table(
        "recommendations",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("opportunity_id", sa.BigInteger(), sa.ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("watchlist_id", sa.BigInteger(), sa.ForeignKey("watchlists.id", ondelete="CASCADE")),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("similarity", sa.Numeric()),
        sa.Column("evidence", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("embedding_model_version", sa.Text()),
        sa.Column("watchlist_profile_hash", sa.Text()),
        sa.Column("opportunity_source_hash", sa.Text()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("uq_recommendations_opp_watchlist_category", "recommendations",
                    ["opportunity_id", sa.text("coalesce(watchlist_id, 0)"), "category"], unique=True)
    op.execute("CREATE TRIGGER trg_recommendations_set_updated_at BEFORE UPDATE ON recommendations "
               "FOR EACH ROW EXECUTE FUNCTION govcon_set_updated_at()")

    op.add_column("matches", sa.Column("rank_score", sa.Numeric()))
    op.add_column("matches", sa.Column("rank_factors", postgresql.JSONB()))
    op.add_column("matches", sa.Column("ranked_at", sa.DateTime(timezone=True)))

    op.add_column("notifications", sa.Column("acknowledged_at", sa.DateTime(timezone=True)))
    op.drop_constraint("ck_notifications_type", "notifications")
    op.create_check_constraint("ck_notifications_type", "notifications", f"notification_type IN ({_NEW_TYPES})")
    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("notification_id", sa.BigInteger(), sa.ForeignKey("notifications.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False, server_default="email"),
        sa.Column("recipient", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('queued','sent','failed')", name="ck_notification_deliveries_status"),
        sa.CheckConstraint("channel IN ('email')", name="ck_notification_deliveries_channel"),
    )
    op.create_index("ix_notification_deliveries_notification", "notification_deliveries", ["notification_id"])
    op.execute("CREATE TRIGGER trg_notification_deliveries_set_updated_at BEFORE UPDATE ON notification_deliveries "
               "FOR EACH ROW EXECUTE FUNCTION govcon_set_updated_at()")

    op.add_column("review_assignments", sa.Column("last_reminded_at", sa.DateTime(timezone=True)))
    op.add_column("review_assignments", sa.Column("escalated_at", sa.DateTime(timezone=True)))

    op.drop_constraint("ck_tasks_task_type", "tasks")
    op.create_check_constraint("ck_tasks_task_type", "tasks", f"task_type IN {_NEW_TASKS}")


def downgrade():
    op.execute("DELETE FROM tasks WHERE task_type = 'notification_email'")
    op.drop_constraint("ck_tasks_task_type", "tasks")
    op.create_check_constraint("ck_tasks_task_type", "tasks", f"task_type IN {_OLD_TASKS}")
    op.drop_column("review_assignments", "escalated_at")
    op.drop_column("review_assignments", "last_reminded_at")
    op.execute("DROP TRIGGER IF EXISTS trg_notification_deliveries_set_updated_at ON notification_deliveries")
    op.drop_table("notification_deliveries")
    op.execute(f"DELETE FROM notifications WHERE notification_type NOT IN ({_OLD_TYPES})")
    op.drop_constraint("ck_notifications_type", "notifications")
    op.create_check_constraint("ck_notifications_type", "notifications", f"notification_type IN ({_OLD_TYPES})")
    op.drop_column("notifications", "acknowledged_at")
    op.drop_column("matches", "ranked_at")
    op.drop_column("matches", "rank_factors")
    op.drop_column("matches", "rank_score")
    op.execute("DROP TRIGGER IF EXISTS trg_recommendations_set_updated_at ON recommendations")
    op.drop_table("recommendations")
