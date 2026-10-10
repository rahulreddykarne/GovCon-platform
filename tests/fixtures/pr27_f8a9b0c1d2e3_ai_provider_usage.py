"""Test fixture: the AI-usage migration as shipped on cursor/e2e-reliability-bots-8adf (commit 442d7a2).

It reused revision f8a9b0c1d2e3, which this branch gives to market_price_runs.
tests/test_migration_paths.py applies it to rebuild a database stamped by that branch.
Not an Alembic revision here: it lives outside alembic/versions.

Provider-reported token usage and editable model prices.

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-10-10
"""

import sqlalchemy as sa

from alembic import op

revision = "f8a9b0c1d2e3"
down_revision = "e7f8a9b0c1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_model_prices",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_usd_per_million", sa.Numeric(), nullable=False),
        sa.Column("output_usd_per_million", sa.Numeric(), nullable=False),
        sa.Column("cached_usd_per_million", sa.Numeric()),
        sa.Column("cache_write_usd_per_million", sa.Numeric()),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("effective_as_of", sa.Date(), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("provider", "model", name="uq_ai_model_prices_provider_model"),
        sa.CheckConstraint(
            "input_usd_per_million >= 0 AND output_usd_per_million >= 0 "
            "AND (cached_usd_per_million IS NULL OR cached_usd_per_million >= 0) "
            "AND (cache_write_usd_per_million IS NULL OR cache_write_usd_per_million >= 0)",
            name="ck_ai_model_prices_nonnegative",
        ),
    )
    op.create_table(
        "ai_provider_calls",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("opportunity_id", sa.BigInteger()),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger()),
        sa.Column("output_tokens", sa.BigInteger()),
        sa.Column("cached_tokens", sa.BigInteger()),
        sa.Column("cache_write_tokens", sa.BigInteger()),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("analysis_id", sa.BigInteger()),
        sa.Column("decision_run_id", sa.BigInteger()),
        sa.Column("cost_usd", sa.Numeric()),
        sa.Column("price_id", sa.BigInteger(), sa.ForeignKey("ai_model_prices.id")),
        sa.CheckConstraint(
            "status IN ('succeeded', 'failed', 'blocked', 'local')",
            name="ck_ai_provider_calls_status",
        ),
        sa.CheckConstraint(
            "(input_tokens IS NULL OR input_tokens >= 0) "
            "AND (output_tokens IS NULL OR output_tokens >= 0) "
            "AND (cached_tokens IS NULL OR cached_tokens >= 0) "
            "AND (cache_write_tokens IS NULL OR cache_write_tokens >= 0) "
            "AND (cost_usd IS NULL OR cost_usd >= 0) "
            "AND (latency_ms IS NULL OR latency_ms >= 0)",
            name="ck_ai_provider_calls_nonnegative",
        ),
    )
    op.create_index("ix_ai_provider_calls_created_at", "ai_provider_calls", ["created_at"])
    op.create_index("ix_ai_provider_calls_opportunity_id", "ai_provider_calls", ["opportunity_id"])
    op.create_index("ix_ai_provider_calls_provider_model", "ai_provider_calls", ["provider", "model"])

    from govcon.ai.price_catalog import SEED_PRICES

    prices = sa.table(
        "ai_model_prices",
        sa.column("provider", sa.Text()),
        sa.column("model", sa.Text()),
        sa.column("input_usd_per_million", sa.Numeric()),
        sa.column("output_usd_per_million", sa.Numeric()),
        sa.column("cached_usd_per_million", sa.Numeric()),
        sa.column("cache_write_usd_per_million", sa.Numeric()),
        sa.column("source_url", sa.Text()),
        sa.column("effective_as_of", sa.Date()),
        sa.column("note", sa.Text()),
    )
    # This copy predates the web search rate column; seed only the columns it created.
    op.bulk_insert(prices, [{k: v for k, v in row.items() if k != "web_search_usd_per_thousand"}
                            for row in SEED_PRICES])


def downgrade() -> None:
    op.drop_index("ix_ai_provider_calls_provider_model", table_name="ai_provider_calls")
    op.drop_index("ix_ai_provider_calls_opportunity_id", table_name="ai_provider_calls")
    op.drop_index("ix_ai_provider_calls_created_at", table_name="ai_provider_calls")
    op.drop_table("ai_provider_calls")
    op.drop_table("ai_model_prices")
