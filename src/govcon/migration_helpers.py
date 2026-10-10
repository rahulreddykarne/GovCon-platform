"""Idempotent schema steps shared by Alembic revisions.

Two branches once shipped different migrations under revision ``f8a9b0c1d2e3``
(market price runs on one, AI provider usage on the other). A database stamped
with either one must reach the same schema, so these steps check what exists
before creating it and are safe to run again.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

TASK_TYPES_WITH_MARKET_PRICES = (
    "proposal_generation", "ai_analysis", "solicitation_summary", "scheduler_chain", "opportunity_preparation",
    "notification_email", "quote_extraction", "bot_run", "market_price_research",
)


def _inspector() -> sa.Inspector:
    # A fresh inspector each time: the cached one does not see objects created earlier in this migration.
    return sa.inspect(op.get_bind())


def has_table(name: str) -> bool:
    return _inspector().has_table(name)


def has_column(table: str, column: str) -> bool:
    return has_table(table) and any(col["name"] == column for col in _inspector().get_columns(table))


def has_index(table: str, index: str) -> bool:
    return has_table(table) and any(item["name"] == index for item in _inspector().get_indexes(table))


def constraint_definition(table: str, name: str) -> str | None:
    """``pg_get_constraintdef`` text for a named constraint, or None when it does not exist."""
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


def ensure_table(name: str, *columns: Any) -> bool:
    """Create ``name`` when it is missing. Returns True when it was created."""
    if has_table(name):
        return False
    op.create_table(name, *columns)
    return True


def ensure_column(table: str, column: sa.Column) -> bool:
    if has_column(table, str(column.name)):
        return False
    op.add_column(table, column)
    return True


def ensure_index(name: str, table: str, columns: Sequence[str]) -> bool:
    if has_index(table, name):
        return False
    op.create_index(name, table, list(columns))
    return True


def ensure_check_constraint(table: str, name: str, condition: str, *, accept: Callable[[str], bool] | None = None) -> bool:
    """Create or replace a CHECK constraint.

    An existing constraint is kept when ``accept`` says its definition is
    already right; otherwise it is dropped and recreated with ``condition``.
    """
    existing = constraint_definition(table, name)
    if existing is not None and (accept is None or accept(existing)):
        return False
    if existing is not None:
        op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, condition)
    return True


def ensure_market_price_runs() -> None:
    """The ``market_price_runs`` table, its index, and the task type that fills it."""
    ensure_table(
        "market_price_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("opportunity_id", sa.BigInteger(), sa.ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False),
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
    ensure_index("ix_market_price_runs_opportunity_created", "market_price_runs", ["opportunity_id", "created_at"])
    allowed = ",".join(f"'{name}'" for name in TASK_TYPES_WITH_MARKET_PRICES)
    ensure_check_constraint(
        "tasks", "ck_tasks_task_type", f"task_type IN ({allowed})",
        accept=lambda definition: "market_price_research" in definition,
    )
