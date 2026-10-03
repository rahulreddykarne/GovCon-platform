"""Durable AI budgets and explicit source-document classification."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "7f4d81a9c630"
down_revision = "01a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("files", sa.Column("classification", sa.Text(), nullable=False, server_default="UNKNOWN"))
    op.add_column("files", sa.Column("source_origin", sa.Text(), nullable=False, server_default="legacy_unknown"))
    # Only known public-feed downloads are grandfathered as public. Local
    # imports and unknown origins remain blocked under the default AI policy.
    op.execute("""UPDATE files SET classification='PUBLIC', source_origin='government_feed'
        FROM opportunities o WHERE files.opportunity_id=o.id AND files.url IS NOT NULL
        AND o.source IN ('sam', 'dibbs')""")
    op.create_check_constraint("ck_files_classification", "files", "classification IN ('PUBLIC','PROPRIETARY','FCI','CUI','UNKNOWN','SECRET_CREDENTIAL')")
    op.create_check_constraint("ck_files_source_origin", "files", "length(trim(source_origin)) BETWEEN 1 AND 200")
    op.create_table("ai_call_usage",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("opportunity_id", sa.BigInteger()),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cost_usd", sa.Numeric()),
        sa.Column("usage", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("input_tokens >= 0 AND output_tokens >= 0 AND (cost_usd IS NULL OR cost_usd >= 0)", name="ck_ai_call_usage_nonnegative"),
        sa.CheckConstraint("status IN ('reserved','succeeded','failed')", name="ck_ai_call_usage_status"),
    )
    op.create_index("ix_ai_call_usage_opportunity_id", "ai_call_usage", ["opportunity_id"])


def downgrade():
    op.drop_table("ai_call_usage")
    op.drop_constraint("ck_files_source_origin", "files")
    op.drop_constraint("ck_files_classification", "files")
    op.drop_column("files", "source_origin")
    op.drop_column("files", "classification")
