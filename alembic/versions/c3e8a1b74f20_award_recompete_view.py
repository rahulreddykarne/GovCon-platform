"""award recompete view

Revision ID: c3e8a1b74f20
Revises: 97cb081e9a8e
Create Date: 2026-09-26 20:20:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3e8a1b74f20"
down_revision: Union[str, Sequence[str], None] = "97cb081e9a8e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION govcon_award_period_end(raw jsonb) RETURNS date
        LANGUAGE plpgsql
        IMMUTABLE
        AS $$
        DECLARE
            raw_end text;
        BEGIN
            raw_end := raw->>'End Date';
            IF raw_end IS NULL OR raw_end !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}' THEN
                RETURN NULL;
            END IF;
            RETURN substring(raw_end FROM 1 FOR 10)::date;
        EXCEPTION
            WHEN others THEN
                RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE VIEW award_recompete_candidates AS
        SELECT
            id,
            source,
            award_id,
            piid,
            description,
            psc_code,
            naics_code,
            nsn,
            recipient_uei,
            recipient_name,
            awarding_agency,
            action_date,
            total_obligation,
            quantity,
            unit_price,
            govcon_award_period_end(raw) AS period_end,
            'older_award'::text AS heuristic
        FROM awards
        WHERE action_date IS NOT NULL
          AND action_date <= (CURRENT_DATE - INTERVAL '18 months')
          AND (
                govcon_award_period_end(raw) IS NULL
                OR govcon_award_period_end(raw) <= (CURRENT_DATE + INTERVAL '18 months')
              )
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS award_recompete_candidates")
    op.execute("DROP FUNCTION IF EXISTS govcon_award_period_end(jsonb)")
