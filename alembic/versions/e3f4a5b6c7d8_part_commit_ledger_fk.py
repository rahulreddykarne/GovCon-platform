"""Commit AI parts independently and allow opportunity_review.

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-10-10
"""
from govcon.migration_helpers import ensure_part_commit_v1

revision = "e3f4a5b6c7d8"
down_revision = "d2e3f4a5b6c7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ensure_part_commit_v1()


def downgrade() -> None:
    return
