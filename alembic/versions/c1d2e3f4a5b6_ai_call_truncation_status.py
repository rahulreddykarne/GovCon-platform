"""Record truncated and discarded provider calls without calling them succeeded.

Revision ID: c1d2e3f4a5b6
Revises: b0c1d2e3f4a5
Create Date: 2026-10-10
"""
from govcon.migration_helpers import ensure_ai_usage_v2

revision = "c1d2e3f4a5b6"
down_revision = "b0c1d2e3f4a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ensure_ai_usage_v2()


def downgrade() -> None:
    # Status values and columns are additive; a downgrade would reject stored
    # truncated/output_rejected rows, so the widened schema is left in place.
    return
