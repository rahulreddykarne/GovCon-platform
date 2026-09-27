"""phase15 outcome learning — structured outcome fields

Revision ID: b5c6d7e8f9a0
Revises: f2a3b4c5d6e7
Create Date: 2026-09-27

Adds structured capture columns to outcome_feedback per §21 of MASTER_SPEC_v2.5.md.
All columns are nullable so existing rows are unaffected.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b5c6d7e8f9a0"
down_revision: Union[str, Sequence[str], None] = "f2a3b4c5d6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Structured no-bid reason category (enum-like code alongside free-text no_bid_reason)
    op.add_column(
        "outcome_feedback",
        sa.Column("no_bid_category", sa.Text(), nullable=True),
    )
    # Known winning price (for lost — distinct from award_amount which is confirmed award)
    op.add_column(
        "outcome_feedback",
        sa.Column("known_winning_price", sa.Numeric(), nullable=True),
    )
    # Win structured capture
    op.add_column(
        "outcome_feedback",
        sa.Column("win_margin_pct", sa.Numeric(), nullable=True),
    )
    op.add_column(
        "outcome_feedback",
        sa.Column("win_supplier", sa.Text(), nullable=True),
    )
    op.add_column(
        "outcome_feedback",
        sa.Column("win_delivery_terms", sa.Text(), nullable=True),
    )
    op.add_column(
        "outcome_feedback",
        sa.Column("win_proposal_version", sa.Text(), nullable=True),
    )
    # Denormalized opportunity fields at time of recording (for analytics without joins)
    op.add_column(
        "outcome_feedback",
        sa.Column("denorm_agency", sa.Text(), nullable=True),
    )
    op.add_column(
        "outcome_feedback",
        sa.Column("denorm_psc", sa.Text(), nullable=True),
    )
    op.add_column(
        "outcome_feedback",
        sa.Column("denorm_naics", sa.Text(), nullable=True),
    )
    op.add_column(
        "outcome_feedback",
        sa.Column("denorm_estimated_value", sa.Numeric(), nullable=True),
    )
    # FK to the AI analysis row that classified this outcome (nullable — AI optional)
    op.add_column(
        "outcome_feedback",
        sa.Column(
            "outcome_analysis_id",
            sa.BigInteger(),
            sa.ForeignKey("ai_analyses.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("outcome_feedback", "outcome_analysis_id")
    op.drop_column("outcome_feedback", "denorm_estimated_value")
    op.drop_column("outcome_feedback", "denorm_naics")
    op.drop_column("outcome_feedback", "denorm_psc")
    op.drop_column("outcome_feedback", "denorm_agency")
    op.drop_column("outcome_feedback", "win_proposal_version")
    op.drop_column("outcome_feedback", "win_delivery_terms")
    op.drop_column("outcome_feedback", "win_supplier")
    op.drop_column("outcome_feedback", "win_margin_pct")
    op.drop_column("outcome_feedback", "known_winning_price")
    op.drop_column("outcome_feedback", "no_bid_category")
