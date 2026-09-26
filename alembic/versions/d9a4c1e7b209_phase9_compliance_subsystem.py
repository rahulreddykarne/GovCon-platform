"""phase9 compliance subsystem

Adds the Phase 9 columns to the Phase 0 compliance tables: per-pass source
references, reconciliation metadata, parsed key values, validation trail,
blocking flags, clause links, optimistic versioning on requirements, run
status/warnings on compliance_runs, and run link/certainty on findings.

Revision ID: d9a4c1e7b209
Revises: c3e8a1b74f20
Create Date: 2026-09-26 21:31:33.226987

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d9a4c1e7b209"
down_revision: Union[str, Sequence[str], None] = "c3e8a1b74f20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("compliance_runs", sa.Column("status", sa.Text(), server_default=sa.text("'complete'"), nullable=False))
    op.add_column("compliance_runs", sa.Column("warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_index(
        "ix_compliance_runs_opportunity_type_created",
        "compliance_runs",
        ["opportunity_id", "run_type", "created_at"],
    )

    op.add_column("compliance_findings", sa.Column("compliance_run_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "compliance_findings",
        sa.Column("blocks_submission", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("compliance_findings", sa.Column("certainty", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_compliance_findings_compliance_run_id",
        "compliance_findings",
        "compliance_runs",
        ["compliance_run_id"],
        ["id"],
    )
    op.create_index(
        "ix_compliance_findings_opportunity_status",
        "compliance_findings",
        ["opportunity_id", "status"],
    )

    op.add_column("requirements", sa.Column("source_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("requirements", sa.Column("reconciliation", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("requirements", sa.Column("key_values", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("requirements", sa.Column("validation", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("requirements", sa.Column("status_reason", sa.Text(), nullable=True))
    op.add_column(
        "requirements",
        sa.Column("blocks_submission", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("requirements", sa.Column("clause_library_id", sa.BigInteger(), nullable=True))
    op.add_column("requirements", sa.Column("compliance_run_id", sa.BigInteger(), nullable=True))
    op.add_column("requirements", sa.Column("amendment_changed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("requirements", sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False))
    op.create_foreign_key(
        "fk_requirements_compliance_run_id",
        "requirements",
        "compliance_runs",
        ["compliance_run_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_requirements_clause_library_id",
        "requirements",
        "clause_library",
        ["clause_library_id"],
        ["id"],
    )
    op.create_check_constraint(
        "ck_requirements_severity",
        "requirements",
        "severity IS NULL OR severity IN ('critical', 'high', 'medium', 'low')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_requirements_severity", "requirements", type_="check")
    op.drop_constraint("fk_requirements_clause_library_id", "requirements", type_="foreignkey")
    op.drop_constraint("fk_requirements_compliance_run_id", "requirements", type_="foreignkey")
    for column in (
        "version",
        "amendment_changed_at",
        "compliance_run_id",
        "clause_library_id",
        "blocks_submission",
        "status_reason",
        "validation",
        "key_values",
        "reconciliation",
        "source_refs",
    ):
        op.drop_column("requirements", column)

    op.drop_index("ix_compliance_findings_opportunity_status", table_name="compliance_findings")
    op.drop_constraint("fk_compliance_findings_compliance_run_id", "compliance_findings", type_="foreignkey")
    op.drop_column("compliance_findings", "certainty")
    op.drop_column("compliance_findings", "blocks_submission")
    op.drop_column("compliance_findings", "compliance_run_id")

    op.drop_index("ix_compliance_runs_opportunity_type_created", table_name="compliance_runs")
    op.drop_column("compliance_runs", "warnings")
    op.drop_column("compliance_runs", "status")
