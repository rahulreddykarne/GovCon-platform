"""Award-match outcome suggestions and analytics snapshots (ADR-073, ADR-074).

Revision ID: a3b4c5d6e7f8
Revises: e1f2a3b4c5d6
Create Date: 2026-10-03
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "a3b4c5d6e7f8"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "outcome_suggestions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("opportunity_id", sa.BigInteger(), sa.ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("strength", sa.Text(), nullable=False),
        sa.Column("suggested_outcome", sa.Text()),
        sa.Column("matched_identifiers", postgresql.JSONB(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("awardee_name", sa.Text()),
        sa.Column("awardee_uei", sa.Text()),
        sa.Column("award_amount", sa.Numeric()),
        sa.Column("award_date", sa.Date()),
        sa.Column("status", sa.Text(), nullable=False, server_default="suggested"),
        sa.Column("decided_by_user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("opportunity_id", "source", "source_ref", name="uq_outcome_suggestions_source"),
        sa.CheckConstraint("source IN ('sam_award_notice','usaspending')", name="ck_outcome_suggestions_source"),
        sa.CheckConstraint("strength IN ('strong','possible')", name="ck_outcome_suggestions_strength"),
        sa.CheckConstraint("suggested_outcome IS NULL OR suggested_outcome IN ('won','lost')",
                           name="ck_outcome_suggestions_outcome"),
        sa.CheckConstraint("status IN ('suggested','confirmed','dismissed')", name="ck_outcome_suggestions_status"),
    )
    op.execute("CREATE TRIGGER trg_outcome_suggestions_set_updated_at BEFORE UPDATE ON outcome_suggestions "
               "FOR EACH ROW EXECUTE FUNCTION govcon_set_updated_at()")
    op.create_table(
        "analytics_snapshots",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("outcome_count", sa.Integer(), nullable=False),
        sa.Column("won", sa.Integer(), nullable=False),
        sa.Column("lost", sa.Integer(), nullable=False),
        sa.Column("no_bid", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table("analytics_snapshots")
    op.execute("DROP TRIGGER IF EXISTS trg_outcome_suggestions_set_updated_at ON outcome_suggestions")
    op.drop_table("outcome_suggestions")
