"""Provider-reported token usage, server-tool counts, and editable model prices.

Every step checks what exists first. A database stamped ``f8a9b0c1d2e3`` by
the earlier AI-usage branch already has ``ai_model_prices`` and
``ai_provider_calls`` but never got ``market_price_runs``; this revision adds
the missing table, the market price task type, and the new server-tool
columns, and keeps the usage rows it already has. A fresh database gets
everything from scratch.

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4
Create Date: 2026-10-10
"""

import sqlalchemy as sa
from alembic import op

from govcon.migration_helpers import (
    ensure_check_constraint,
    ensure_column,
    ensure_index,
    ensure_market_price_runs,
    ensure_table,
    has_index,
    has_table,
)

revision = "b0c1d2e3f4a5"
down_revision = "a9b0c1d2e3f4"
branch_labels = None
depends_on = None

_PRICE_CHECK = (
    "input_usd_per_million >= 0 AND output_usd_per_million >= 0 "
    "AND (cached_usd_per_million IS NULL OR cached_usd_per_million >= 0) "
    "AND (cache_write_usd_per_million IS NULL OR cache_write_usd_per_million >= 0) "
    "AND (web_search_usd_per_thousand IS NULL OR web_search_usd_per_thousand >= 0)"
)
_CALL_STATUS = "status IN ('succeeded', 'failed', 'blocked', 'local')"
_CALL_CHECK = (
    "(input_tokens IS NULL OR input_tokens >= 0) "
    "AND (output_tokens IS NULL OR output_tokens >= 0) "
    "AND (cached_tokens IS NULL OR cached_tokens >= 0) "
    "AND (cache_write_tokens IS NULL OR cache_write_tokens >= 0) "
    "AND (web_search_requests IS NULL OR web_search_requests >= 0) "
    "AND (web_fetch_requests IS NULL OR web_fetch_requests >= 0) "
    "AND (cost_usd IS NULL OR cost_usd >= 0) "
    "AND (latency_ms IS NULL OR latency_ms >= 0)"
)


def upgrade() -> None:
    # Repair: a database stamped f8a9b0c1d2e3 by the AI-usage branch skipped this.
    ensure_market_price_runs()

    ensure_table(
        "ai_model_prices",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_usd_per_million", sa.Numeric(), nullable=False),
        sa.Column("output_usd_per_million", sa.Numeric(), nullable=False),
        sa.Column("cached_usd_per_million", sa.Numeric()),
        sa.Column("cache_write_usd_per_million", sa.Numeric()),
        sa.Column("web_search_usd_per_thousand", sa.Numeric()),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("effective_as_of", sa.Date(), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("provider", "model", name="uq_ai_model_prices_provider_model"),
    )
    ensure_column("ai_model_prices", sa.Column("web_search_usd_per_thousand", sa.Numeric()))
    ensure_check_constraint("ai_model_prices", "ck_ai_model_prices_nonnegative", _PRICE_CHECK,
                            accept=lambda definition: "web_search_usd_per_thousand" in definition)

    ensure_table(
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
        sa.Column("web_search_requests", sa.BigInteger()),
        sa.Column("web_fetch_requests", sa.BigInteger()),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("analysis_id", sa.BigInteger()),
        sa.Column("decision_run_id", sa.BigInteger()),
        sa.Column("cost_usd", sa.Numeric()),
        sa.Column("price_id", sa.BigInteger(), sa.ForeignKey("ai_model_prices.id")),
    )
    ensure_column("ai_provider_calls", sa.Column("web_search_requests", sa.BigInteger()))
    ensure_column("ai_provider_calls", sa.Column("web_fetch_requests", sa.BigInteger()))
    ensure_check_constraint("ai_provider_calls", "ck_ai_provider_calls_status", _CALL_STATUS)
    ensure_check_constraint("ai_provider_calls", "ck_ai_provider_calls_nonnegative", _CALL_CHECK,
                            accept=lambda definition: "web_fetch_requests" in definition)
    ensure_index("ix_ai_provider_calls_created_at", "ai_provider_calls", ["created_at"])
    ensure_index("ix_ai_provider_calls_opportunity_id", "ai_provider_calls", ["opportunity_id"])
    ensure_index("ix_ai_provider_calls_provider_model", "ai_provider_calls", ["provider", "model"])

    _seed_prices()


def _seed_prices() -> None:
    """Insert cited prices that are not stored yet; fill only a new, empty web search rate."""
    from govcon.ai.price_catalog import SEED_PRICES

    bind = op.get_bind()
    for row in SEED_PRICES:
        bind.execute(
            sa.text(
                "INSERT INTO ai_model_prices (provider, model, input_usd_per_million, output_usd_per_million, "
                "cached_usd_per_million, cache_write_usd_per_million, web_search_usd_per_thousand, source_url, "
                "effective_as_of, note) VALUES (:provider, :model, :input_usd_per_million, :output_usd_per_million, "
                ":cached_usd_per_million, :cache_write_usd_per_million, :web_search_usd_per_thousand, :source_url, "
                ":effective_as_of, :note) ON CONFLICT ON CONSTRAINT uq_ai_model_prices_provider_model DO NOTHING"
            ),
            dict(row),
        )
        if row.get("web_search_usd_per_thousand") is not None:
            bind.execute(
                sa.text(
                    "UPDATE ai_model_prices SET web_search_usd_per_thousand = :rate "
                    "WHERE provider = :provider AND model = :model AND web_search_usd_per_thousand IS NULL"
                ),
                {"rate": row["web_search_usd_per_thousand"], "provider": row["provider"], "model": row["model"]},
            )


def downgrade() -> None:
    for name in ("ix_ai_provider_calls_provider_model", "ix_ai_provider_calls_opportunity_id",
                 "ix_ai_provider_calls_created_at"):
        if has_index("ai_provider_calls", name):
            op.drop_index(name, table_name="ai_provider_calls")
    if has_table("ai_provider_calls"):
        op.drop_table("ai_provider_calls")
    if has_table("ai_model_prices"):
        op.drop_table("ai_model_prices")
    # market_price_runs belongs to f8a9b0c1d2e3 and is left for its own downgrade.
