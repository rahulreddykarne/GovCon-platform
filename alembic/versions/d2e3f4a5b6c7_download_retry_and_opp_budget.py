"""Download-only SAM retry and per-opportunity token budget override.

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-10-10
"""
from govcon.migration_helpers import ensure_download_retry_v1

revision = "d2e3f4a5b6c7"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ensure_download_retry_v1()


def downgrade() -> None:
    # Additive check/column; leaving them in place avoids rejecting stored rows.
    return
