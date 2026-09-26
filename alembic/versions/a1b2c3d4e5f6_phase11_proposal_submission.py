"""phase11 proposal submission

Adds Phase 11 columns to proposals and submissions:
- proposals.status CheckConstraint and final-approval tracking columns
- proposals.red_team_analysis_id FK to ai_analyses
- proposals.submission_id FK to submissions
- submissions: no new columns needed (schema already complete)
- notification_type extended to cover proposal_package_generated and submission_ready
  (already present in Phase 0 model; no migration needed)

Revision ID: a1b2c3d4e5f6
Revises: d9a4c1e7b209
Create Date: 2026-09-26 23:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "d9a4c1e7b209"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add final-approval tracking columns to proposals
    op.add_column("proposals", sa.Column("final_approved_by_user_id", sa.BigInteger(), nullable=True))
    op.add_column("proposals", sa.Column("final_approved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("proposals", sa.Column("red_team_analysis_id", sa.BigInteger(), nullable=True))
    op.add_column("proposals", sa.Column("submission_id", sa.BigInteger(), nullable=True))

    # Add FK constraints
    op.create_foreign_key(
        "fk_proposals_final_approved_by_user_id",
        "proposals",
        "users",
        ["final_approved_by_user_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_proposals_red_team_analysis_id",
        "proposals",
        "ai_analyses",
        ["red_team_analysis_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_proposals_submission_id",
        "proposals",
        "submissions",
        ["submission_id"],
        ["id"],
    )

    # Add CheckConstraint on proposals.status
    op.create_check_constraint(
        "ck_proposals_status",
        "proposals",
        "status IN ('draft', 'ai_generated', 'red_teamed', 'final_approved', 'returned_for_fix', 'cancelled')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_proposals_status", "proposals", type_="check")
    op.drop_constraint("fk_proposals_submission_id", "proposals", type_="foreignkey")
    op.drop_constraint("fk_proposals_red_team_analysis_id", "proposals", type_="foreignkey")
    op.drop_constraint("fk_proposals_final_approved_by_user_id", "proposals", type_="foreignkey")
    op.drop_column("proposals", "submission_id")
    op.drop_column("proposals", "red_team_analysis_id")
    op.drop_column("proposals", "final_approved_at")
    op.drop_column("proposals", "final_approved_by_user_id")
