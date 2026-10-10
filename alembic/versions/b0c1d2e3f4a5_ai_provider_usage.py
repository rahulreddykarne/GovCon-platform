"""Provider-reported token usage, web search/fetch counts and editable model prices.

This schema first shipped on another branch under revision id
``f8a9b0c1d2e3``, which this branch uses for ``market_price_runs``. It now has
its own id. A database that ran the earlier migration already has both tables
and the seeded prices: missing columns, constraints and indexes are added,
stored calls are kept, and only prices that are not stored are inserted.

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4
Create Date: 2026-10-10
"""
from alembic import op

from govcon.migration_helpers import ensure_ai_usage_v1, seed_model_prices, table_exists

revision = "b0c1d2e3f4a5"
down_revision = "a9b0c1d2e3f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from govcon.ai.price_catalog import SEED_PRICES

    ensure_ai_usage_v1()
    seed_model_prices(SEED_PRICES)


def downgrade() -> None:
    if table_exists("ai_provider_calls"):
        op.drop_table("ai_provider_calls")
    if table_exists("ai_model_prices"):
        op.drop_table("ai_model_prices")
