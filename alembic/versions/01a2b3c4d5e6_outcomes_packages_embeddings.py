"""Unique current outcomes/submissions, immutable history and embedding provenance.

Revision ID: 01a2b3c4d5e6
Revises: f0a1b2c3d4e5
"""
from alembic import op
import sqlalchemy as sa

revision = "01a2b3c4d5e6"
down_revision = "f0a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("outcome_corrections",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("opportunity_id", sa.BigInteger(), sa.ForeignKey("opportunities.id"), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("old_value", sa.dialects.postgresql.JSONB()),
        sa.Column("new_value", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")))
    # Preserve every legacy outcome, including superseded rows and invalid amounts.
    op.execute("INSERT INTO outcome_corrections (opportunity_id, new_value, created_at) SELECT opportunity_id, to_jsonb(f), coalesce(updated_at, created_at, now()) FROM outcome_feedback f ORDER BY id")
    op.execute("DELETE FROM outcome_feedback f USING outcome_feedback newer WHERE f.opportunity_id = newer.opportunity_id AND f.id < newer.id")
    op.execute("UPDATE outcome_feedback SET award_amount = NULL WHERE award_amount < 0 OR award_amount >= 'Infinity'::numeric")
    op.execute("UPDATE outcome_feedback SET known_winning_price = NULL WHERE known_winning_price < 0 OR known_winning_price >= 'Infinity'::numeric")
    op.execute("UPDATE outcome_feedback SET win_margin_pct = NULL WHERE NOT (win_margin_pct >= 0 AND win_margin_pct <= 100)")
    op.execute("UPDATE outcome_feedback SET outcome = NULL WHERE outcome NOT IN ('won', 'lost', 'no_bid', 'cancelled')")
    op.create_unique_constraint("uq_outcome_feedback_opportunity", "outcome_feedback", ["opportunity_id"])
    for name, expression in (
        ("ck_outcome_feedback_outcome", "outcome IN ('won', 'lost', 'no_bid', 'cancelled')"),
        ("ck_outcome_award_amount", "award_amount IS NULL OR (award_amount >= 0 AND award_amount < 'Infinity'::numeric)"),
        ("ck_outcome_winning_price", "known_winning_price IS NULL OR (known_winning_price >= 0 AND known_winning_price < 'Infinity'::numeric)"),
        ("ck_outcome_margin", "win_margin_pct IS NULL OR (win_margin_pct >= 0 AND win_margin_pct <= 100)"),
    ):
        op.create_check_constraint(name, "outcome_feedback", expression)
    op.create_table("submission_legacy_history",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("snapshot", sa.dialects.postgresql.JSONB(), nullable=False))
    op.execute("""INSERT INTO submission_legacy_history (snapshot)
        SELECT to_jsonb(s) || jsonb_build_object('referencing_proposal_ids',
            (SELECT jsonb_agg(p.id) FROM proposals p WHERE p.submission_id = s.id))
        FROM submissions s WHERE opportunity_id IN
            (SELECT opportunity_id FROM submissions GROUP BY opportunity_id HAVING count(*) > 1)""")
    # Retain the row furthest through submission, then newest ID. All others remain archived.
    op.execute("""WITH canonical AS (
        SELECT id, first_value(id) OVER (PARTITION BY opportunity_id ORDER BY
            CASE status WHEN 'confirmed' THEN 3 WHEN 'submitted' THEN 2 ELSE 1 END DESC, id DESC) AS kept_id
        FROM submissions)
        UPDATE proposals p SET submission_id = c.kept_id FROM canonical c
        WHERE p.submission_id = c.id AND c.id <> c.kept_id""")
    op.execute("""DELETE FROM submissions WHERE id IN (
        SELECT id FROM (SELECT id, row_number() OVER (PARTITION BY opportunity_id ORDER BY
            CASE status WHEN 'confirmed' THEN 3 WHEN 'submitted' THEN 2 ELSE 1 END DESC, id DESC) AS n FROM submissions) ranked WHERE n > 1)""")
    op.create_unique_constraint("uq_submissions_opportunity", "submissions", ["opportunity_id"])
    for name in ("assembled_files", "completed_actions"):
        op.add_column("submissions", sa.Column(name, sa.dialects.postgresql.JSONB()))
    op.add_column("submissions", sa.Column("package_manifest_hash", sa.Text()))
    op.create_table("submission_package_manifests",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("submission_id", sa.BigInteger(), sa.ForeignKey("submissions.id"), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("manifest", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("submission_id", "sha256", name="uq_submission_manifest_hash"))
    for table in ("opportunities", "watchlists"):
        op.add_column(table, sa.Column("embedding_model", sa.Text()))
        op.add_column(table, sa.Column("embedding_dimension", sa.Integer()))
        op.add_column(table, sa.Column("embedding_source_hash", sa.Text()))
    op.execute("""CREATE FUNCTION reject_history_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'history is append-only'; END; $$""")
    for table in ("outcome_corrections", "submission_package_manifests", "submission_legacy_history"):
        op.execute(f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()")


def downgrade():
    for table in ("outcome_corrections", "submission_package_manifests", "submission_legacy_history"):
        op.drop_table(table)
    op.execute("DROP FUNCTION reject_history_mutation()")
    for table in ("opportunities", "watchlists"):
        for column in ("embedding_model", "embedding_dimension", "embedding_source_hash"):
            op.drop_column(table, column)
    for column in ("assembled_files", "completed_actions", "package_manifest_hash"):
        op.drop_column("submissions", column)
    op.drop_constraint("uq_submissions_opportunity", "submissions", type_="unique")
    op.drop_constraint("uq_outcome_feedback_opportunity", "outcome_feedback", type_="unique")
    for name in ("ck_outcome_feedback_outcome", "ck_outcome_award_amount", "ck_outcome_winning_price", "ck_outcome_margin"):
        op.drop_constraint(name, "outcome_feedback", type_="check")
