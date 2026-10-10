"""Idempotent DDL for migrations that must repair diverged databases.

Two branches once shipped different migrations under revision ``f8a9b0c1d2e3``
(``market_price_runs`` here, AI provider usage on the other). A database
stamped at that revision by the other branch has the usage tables and lacks
``market_price_runs``, so the later migrations check what exists and create
only what is missing. Each ``ensure_*`` function describes one schema version
and must not change once released; a later schema change is a new function.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


def _inspector() -> sa.engine.reflection.Inspector:
    return sa.inspect(op.get_bind())


def table_exists(name: str) -> bool:
    return _inspector().has_table(name)


def column_exists(table: str, column: str) -> bool:
    return any(col["name"] == column for col in _inspector().get_columns(table))


def index_exists(table: str, name: str) -> bool:
    return any(ix["name"] == name for ix in _inspector().get_indexes(table))


def constraint_definition(table: str, name: str) -> str | None:
    """``pg_get_constraintdef`` text of a named constraint, or None when absent."""
    row = op.get_bind().execute(
        sa.text(
            "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE t.relname = :table AND c.conname = :name AND n.nspname = current_schema()"
        ),
        {"table": table, "name": name},
    ).first()
    return None if row is None else str(row[0])


def add_column_if_missing(table: str, column: sa.Column[Any]) -> bool:
    if column_exists(table, str(column.name)):
        return False
    op.add_column(table, column)
    return True


def create_index_if_missing(name: str, table: str, columns: list[str]) -> bool:
    if index_exists(table, name):
        return False
    op.create_index(name, table, columns)
    return True


def create_check_if_missing(name: str, table: str, condition: str) -> bool:
    if constraint_definition(table, name) is not None:
        return False
    op.create_check_constraint(name, table, condition)
    return True


# ── market_price_runs (revision f8a9b0c1d2e3) ──

MARKET_PRICE_TASK_TYPES_BEFORE = (
    "('proposal_generation','ai_analysis','solicitation_summary','scheduler_chain','opportunity_preparation',"
    "'notification_email','quote_extraction','bot_run')"
)
MARKET_PRICE_TASK_TYPES = (
    "('proposal_generation','ai_analysis','solicitation_summary','scheduler_chain','opportunity_preparation',"
    "'notification_email','quote_extraction','bot_run','market_price_research')"
)


def ensure_market_price_runs_v1() -> None:
    """Create ``market_price_runs`` and allow its task type, skipping what exists."""
    if not table_exists("market_price_runs"):
        op.create_table(
            "market_price_runs",
            sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("opportunity_id", sa.BigInteger(), sa.ForeignKey("opportunities.id", ondelete="CASCADE"),
                      nullable=False),
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
    create_index_if_missing("ix_market_price_runs_opportunity_created", "market_price_runs",
                            ["opportunity_id", "created_at"])
    current = constraint_definition("tasks", "ck_tasks_task_type")
    if current is None or "market_price_research" not in current:
        if current is not None:
            op.drop_constraint("ck_tasks_task_type", "tasks")
        op.create_check_constraint("ck_tasks_task_type", "tasks", f"task_type IN {MARKET_PRICE_TASK_TYPES}")


# ── AI provider usage and model prices (revision b0c1d2e3f4a5) ──

_PRICE_NONNEGATIVE = (
    "input_usd_per_million >= 0 AND output_usd_per_million >= 0 "
    "AND (cached_usd_per_million IS NULL OR cached_usd_per_million >= 0) "
    "AND (cache_write_usd_per_million IS NULL OR cache_write_usd_per_million >= 0) "
    "AND (web_search_usd_per_thousand IS NULL OR web_search_usd_per_thousand >= 0)"
)
_CALL_NONNEGATIVE = (
    "(input_tokens IS NULL OR input_tokens >= 0) "
    "AND (output_tokens IS NULL OR output_tokens >= 0) "
    "AND (cached_tokens IS NULL OR cached_tokens >= 0) "
    "AND (cache_write_tokens IS NULL OR cache_write_tokens >= 0) "
    "AND (web_search_requests IS NULL OR web_search_requests >= 0) "
    "AND (web_fetch_requests IS NULL OR web_fetch_requests >= 0) "
    "AND (cost_usd IS NULL OR cost_usd >= 0) "
    "AND (latency_ms IS NULL OR latency_ms >= 0)"
)
_CALL_STATUS = "status IN ('succeeded', 'failed', 'blocked', 'local')"
_CALL_STATUS_V2 = (
    "status IN ('succeeded', 'failed', 'blocked', 'local', 'truncated', 'output_rejected')"
)


def _replace_check(table: str, name: str, condition: str, required_terms: tuple[str, ...]) -> None:
    """Create the check, or replace an older one that lacks a newer column."""
    current = constraint_definition(table, name)
    if current is not None and all(term in current for term in required_terms):
        return
    if current is not None:
        op.drop_constraint(name, table)
    op.create_check_constraint(name, table, condition)


def ensure_ai_usage_v1() -> None:
    """``ai_model_prices`` and ``ai_provider_calls``, including web search/fetch counts.

    A database that ran the earlier usage migration (no web search columns)
    keeps its rows; the missing columns and constraints are added.
    """
    if not table_exists("ai_model_prices"):
        op.create_table(
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
    else:
        add_column_if_missing("ai_model_prices", sa.Column("web_search_usd_per_thousand", sa.Numeric()))
        if constraint_definition("ai_model_prices", "uq_ai_model_prices_provider_model") is None:
            op.create_unique_constraint("uq_ai_model_prices_provider_model", "ai_model_prices", ["provider", "model"])
    _replace_check("ai_model_prices", "ck_ai_model_prices_nonnegative", _PRICE_NONNEGATIVE,
                   ("web_search_usd_per_thousand",))

    if not table_exists("ai_provider_calls"):
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
            sa.Column("web_search_requests", sa.BigInteger()),
            sa.Column("web_fetch_requests", sa.BigInteger()),
            sa.Column("latency_ms", sa.Integer()),
            sa.Column("analysis_id", sa.BigInteger()),
            sa.Column("decision_run_id", sa.BigInteger()),
            sa.Column("cost_usd", sa.Numeric()),
            sa.Column("price_id", sa.BigInteger(), sa.ForeignKey("ai_model_prices.id")),
        )
    else:
        for name in ("web_search_requests", "web_fetch_requests"):
            add_column_if_missing("ai_provider_calls", sa.Column(name, sa.BigInteger()))
    _replace_check("ai_provider_calls", "ck_ai_provider_calls_status", _CALL_STATUS, ("local",))
    _replace_check("ai_provider_calls", "ck_ai_provider_calls_nonnegative", _CALL_NONNEGATIVE,
                   ("web_search_requests", "web_fetch_requests"))
    create_index_if_missing("ix_ai_provider_calls_created_at", "ai_provider_calls", ["created_at"])
    create_index_if_missing("ix_ai_provider_calls_opportunity_id", "ai_provider_calls", ["opportunity_id"])
    create_index_if_missing("ix_ai_provider_calls_provider_model", "ai_provider_calls", ["provider", "model"])


def ensure_ai_usage_v2() -> None:
    """Widen call status for truncated/discarded answers and store their links.

    ``ensure_ai_usage_v1`` must not change once released. A laptop database that
    already ran v1 keeps its rows; this adds ``finish_reason``,
    ``compliance_run_id``, and the two new status values.
    """
    ensure_ai_usage_v1()
    if table_exists("ai_provider_calls"):
        add_column_if_missing("ai_provider_calls", sa.Column("finish_reason", sa.Text()))
        add_column_if_missing("ai_provider_calls", sa.Column("compliance_run_id", sa.BigInteger()))
        _replace_check(
            "ai_provider_calls",
            "ck_ai_provider_calls_status",
            _CALL_STATUS_V2,
            ("truncated", "output_rejected"),
        )


# ── download-only retry + per-opportunity budget (revision d2e3f4a5b6c7) ──

DOWNLOAD_RETRY_TASK_TYPES = (
    "('proposal_generation','ai_analysis','solicitation_summary','scheduler_chain','opportunity_preparation',"
    "'notification_email','quote_extraction','bot_run','market_price_research','attachment_download')"
)


def ensure_download_retry_v1() -> None:
    """Allow ``attachment_download`` tasks and store a per-opportunity token cap.

    ``ensure_market_price_runs_v1`` must not change once released. A laptop
    database that already ran it keeps its rows; this widens the task-type
    check and adds ``opportunities.ai_max_input_tokens``.
    """
    current = constraint_definition("tasks", "ck_tasks_task_type")
    if current is None or "attachment_download" not in current:
        if current is not None:
            op.drop_constraint("ck_tasks_task_type", "tasks")
        op.create_check_constraint("ck_tasks_task_type", "tasks", f"task_type IN {DOWNLOAD_RETRY_TASK_TYPES}")
    add_column_if_missing("opportunities", sa.Column("ai_max_input_tokens", sa.Integer()))


def seed_model_prices(rows: tuple[dict[str, object], ...]) -> None:
    """Insert cited prices that are not stored yet. Existing (possibly edited) rows are kept.

    A row the earlier usage migration seeded (same source and token rates, no
    web search rate) gets the cited web search fee; an edited row is left alone.
    """
    bind = op.get_bind()
    for row in rows:
        if row.get("web_search_usd_per_thousand") is not None:
            bind.execute(
                sa.text(
                    "UPDATE ai_model_prices SET web_search_usd_per_thousand = :web_search_usd_per_thousand, "
                    "note = :note, updated_at = now() "
                    "WHERE provider = :provider AND model = :model AND web_search_usd_per_thousand IS NULL "
                    "AND source_url = :source_url AND input_usd_per_million = :input_usd_per_million "
                    "AND output_usd_per_million = :output_usd_per_million"
                ),
                row,
            )
        bind.execute(
            sa.text(
                "INSERT INTO ai_model_prices (provider, model, input_usd_per_million, output_usd_per_million, "
                "cached_usd_per_million, cache_write_usd_per_million, web_search_usd_per_thousand, source_url, "
                "effective_as_of, note) VALUES (:provider, :model, :input_usd_per_million, :output_usd_per_million, "
                ":cached_usd_per_million, :cache_write_usd_per_million, :web_search_usd_per_thousand, :source_url, "
                ":effective_as_of, :note) ON CONFLICT (provider, model) DO NOTHING"
            ),
            {"web_search_usd_per_thousand": None, **row},
        )
