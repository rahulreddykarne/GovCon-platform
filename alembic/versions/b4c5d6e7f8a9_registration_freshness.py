"""SAM registration freshness metadata.

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-10-03
"""
import sqlalchemy as sa
from alembic import op

revision = "b4c5d6e7f8a9"
down_revision = "a3b4c5d6e7f8"
branch_labels = None
depends_on = None

_FRESHNESS = "freshness_status IN ('fresh', 'stale', 'expired', 'failed', 'unknown')"
_ATTEMPT = "attempt_state IS NULL OR attempt_state IN ('in_progress', 'cancelled', 'applied', 'failed')"


def upgrade() -> None:
    op.add_column("vendors", sa.Column("source_updated_at", sa.DateTime(timezone=True)))
    op.add_column("vendors", sa.Column("expires_at", sa.Date()))
    op.add_column("vendors", sa.Column("freshness_status", sa.Text(), nullable=False, server_default="unknown"))
    op.add_column("vendors", sa.Column("last_refresh_error", sa.Text()))
    op.add_column("vendors", sa.Column("last_attempt_at", sa.DateTime(timezone=True)))
    op.add_column("vendors", sa.Column("refresh_generation", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("vendors", sa.Column("attempt_id", sa.Text()))
    op.add_column("vendors", sa.Column("attempt_state", sa.Text()))
    op.add_column("vendors", sa.Column("registration_key", sa.Text()))
    op.execute(
        "UPDATE vendors SET freshness_status = 'fresh' "
        "WHERE fetched_at IS NOT NULL AND freshness_status = 'unknown'"
    )
    # Carry the source's own dates forward so an already-expired payload is not
    # treated as a fresh registration after this migration.
    op.execute(
        """
        UPDATE vendors
        SET source_updated_at = CASE
                WHEN (raw #>> '{entityRegistration,lastUpdateDate}') ~ '^\\d{4}-\\d{2}-\\d{2}'
                THEN ((substring(raw #>> '{entityRegistration,lastUpdateDate}' from 1 for 10))::date)::timestamptz
                ELSE source_updated_at
            END,
            expires_at = CASE
                WHEN (raw #>> '{entityRegistration,registrationExpirationDate}') ~ '^\\d{4}-\\d{2}-\\d{2}$'
                THEN (raw #>> '{entityRegistration,registrationExpirationDate}')::date
                ELSE expires_at
            END
        WHERE raw IS NOT NULL
        """
    )
    op.execute(
        "UPDATE vendors SET freshness_status = 'expired' "
        "WHERE expires_at IS NOT NULL AND expires_at < CURRENT_DATE AND freshness_status = 'fresh'"
    )
    op.create_check_constraint("ck_vendors_freshness_status", "vendors", _FRESHNESS)
    op.create_check_constraint("ck_vendors_attempt_state", "vendors", _ATTEMPT)

    op.add_column("company_registration", sa.Column("fetched_at", sa.DateTime(timezone=True)))
    op.add_column("company_registration", sa.Column("source_updated_at", sa.DateTime(timezone=True)))
    op.add_column("company_registration", sa.Column("expires_at", sa.Date()))
    op.add_column("company_registration", sa.Column("freshness_status", sa.Text(), nullable=False, server_default="unknown"))
    op.add_column("company_registration", sa.Column("last_error", sa.Text()))
    op.add_column("company_registration", sa.Column("last_attempt_at", sa.DateTime(timezone=True)))
    op.add_column("company_registration", sa.Column("refresh_generation", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("company_registration", sa.Column("attempt_id", sa.Text()))
    op.add_column("company_registration", sa.Column("attempt_state", sa.Text()))
    op.add_column("company_registration", sa.Column("registration_key", sa.Text()))
    op.execute(
        "UPDATE company_registration SET fetched_at = refreshed_at, freshness_status = 'fresh' "
        "WHERE refreshed_at IS NOT NULL AND freshness_status = 'unknown'"
    )
    op.execute(
        """
        UPDATE company_registration
        SET source_updated_at = CASE
                WHEN (raw #>> '{entityRegistration,lastUpdateDate}') ~ '^\\d{4}-\\d{2}-\\d{2}'
                THEN ((substring(raw #>> '{entityRegistration,lastUpdateDate}' from 1 for 10))::date)::timestamptz
                ELSE source_updated_at
            END,
            expires_at = COALESCE(
                expires_at,
                CASE
                    WHEN (raw #>> '{entityRegistration,registrationExpirationDate}') ~ '^\\d{4}-\\d{2}-\\d{2}$'
                    THEN (raw #>> '{entityRegistration,registrationExpirationDate}')::date
                    ELSE expiration_date
                END
            )
        WHERE raw IS NOT NULL OR expiration_date IS NOT NULL
        """
    )
    op.execute(
        "UPDATE company_registration SET freshness_status = 'expired' "
        "WHERE expires_at IS NOT NULL AND expires_at < CURRENT_DATE AND freshness_status = 'fresh'"
    )
    op.create_check_constraint("ck_company_registration_freshness_status", "company_registration", _FRESHNESS)
    op.create_check_constraint("ck_company_registration_attempt_state", "company_registration", _ATTEMPT)


def downgrade() -> None:
    op.drop_constraint("ck_company_registration_attempt_state", "company_registration", type_="check")
    op.drop_constraint("ck_company_registration_freshness_status", "company_registration", type_="check")
    for name in (
        "registration_key", "attempt_state", "attempt_id", "refresh_generation", "last_attempt_at",
        "last_error", "freshness_status", "expires_at", "source_updated_at", "fetched_at",
    ):
        op.drop_column("company_registration", name)
    op.drop_constraint("ck_vendors_attempt_state", "vendors", type_="check")
    op.drop_constraint("ck_vendors_freshness_status", "vendors", type_="check")
    for name in (
        "registration_key", "attempt_state", "attempt_id", "refresh_generation", "last_attempt_at",
        "last_refresh_error", "freshness_status", "expires_at", "source_updated_at",
    ):
        op.drop_column("vendors", name)
