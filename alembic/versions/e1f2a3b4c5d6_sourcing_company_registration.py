"""Sourcing records and company registration refresh (ADR-071, ADR-072).

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-10-03
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "e1f2a3b4c5d6"
down_revision = "d0e1f2a3b4c5"
branch_labels = None
depends_on = None

_OLD_TASKS = (
    "('proposal_generation','ai_analysis','solicitation_summary','scheduler_chain','opportunity_preparation',"
    "'notification_email')"
)
_NEW_TASKS = (
    "('proposal_generation','ai_analysis','solicitation_summary','scheduler_chain','opportunity_preparation',"
    "'notification_email','quote_extraction')"
)
_TABLES_WITH_UPDATED_AT = ("suppliers", "products", "supplier_products", "supplier_quotes", "rfq_drafts",
                           "company_registration")


def _ts(name, **kw):
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def _stamps():
    return [_ts("created_at", nullable=False, server_default=sa.func.now()),
            _ts("updated_at", nullable=False, server_default=sa.func.now())]


def upgrade():
    op.create_table(
        "suppliers",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("uei", sa.Text()),
        sa.Column("cage_code", sa.Text()),
        sa.Column("contact_name", sa.Text()),
        sa.Column("contact_email", sa.Text()),
        sa.Column("phone", sa.Text()),
        sa.Column("notes", sa.Text()),
        sa.Column("provenance", sa.Text(), nullable=False),
        sa.Column("created_by_user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        *_stamps(),
    )
    op.create_index("uq_suppliers_name", "suppliers", [sa.text("lower(name)")], unique=True)
    op.create_table(
        "products",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("part_number", sa.Text(), nullable=False),
        sa.Column("manufacturer", sa.Text()),
        sa.Column("nsn", sa.Text()),
        sa.Column("description", sa.Text()),
        sa.Column("unit", sa.Text()),
        *_stamps(),
    )
    op.create_index("uq_products_manufacturer_part", "products",
                    [sa.text("coalesce(lower(manufacturer), '')"), sa.text("lower(part_number)")], unique=True)
    op.create_index("ix_products_nsn", "products", ["nsn"])
    op.create_table(
        "catalog_imports",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("supplier_id", sa.BigInteger(), sa.ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.Text()),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("rows_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_imported", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errors", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("imported_by_user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "supplier_products",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("supplier_id", sa.BigInteger(), sa.ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", sa.BigInteger(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("list_price", sa.Numeric()),
        sa.Column("currency", sa.Text(), nullable=False, server_default="USD"),
        sa.Column("valid_until", sa.Date()),
        sa.Column("catalog_import_id", sa.BigInteger(), sa.ForeignKey("catalog_imports.id", ondelete="SET NULL")),
        *_stamps(),
        sa.UniqueConstraint("supplier_id", "product_id", name="uq_supplier_products_supplier_product"),
    )
    op.create_table(
        "supplier_quotes",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("opportunity_id", sa.BigInteger(), sa.ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("supplier_id", sa.BigInteger(), sa.ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("valid_until", sa.Date()),
        sa.Column("total_price", sa.Numeric()),
        sa.Column("currency", sa.Text(), nullable=False, server_default="USD"),
        sa.Column("extraction_method", sa.Text(), nullable=False),
        sa.Column("source_filename", sa.Text()),
        sa.Column("source_sha256", sa.Text()),
        sa.Column("source_key", sa.Text()),
        sa.Column("entered_by_user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("notes", sa.Text()),
        *_stamps(),
        sa.CheckConstraint("status IN ('active','withdrawn')", name="ck_supplier_quotes_status"),
        sa.CheckConstraint("extraction_method IN ('manual','csv','xlsx','ai')", name="ck_supplier_quotes_extraction_method"),
    )
    op.create_index("ix_supplier_quotes_opportunity", "supplier_quotes", ["opportunity_id"])
    op.create_table(
        "supplier_quote_lines",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("quote_id", sa.BigInteger(), sa.ForeignKey("supplier_quotes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", sa.BigInteger(), sa.ForeignKey("products.id", ondelete="SET NULL")),
        sa.Column("description", sa.Text()),
        sa.Column("part_number", sa.Text()),
        sa.Column("nsn", sa.Text()),
        sa.Column("quantity", sa.Numeric()),
        sa.Column("unit", sa.Text()),
        sa.Column("unit_price", sa.Numeric()),
        sa.Column("extended_price", sa.Numeric()),
        sa.Column("lead_time_days", sa.Integer()),
    )
    op.create_table(
        "rfq_drafts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("opportunity_id", sa.BigInteger(), sa.ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("supplier_id", sa.BigInteger(), sa.ForeignKey("suppliers.id", ondelete="SET NULL")),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("created_by_user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        *_stamps(),
        sa.CheckConstraint("status IN ('draft')", name="ck_rfq_drafts_status"),
    )
    op.create_table(
        "ai_sharing_authorizations",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("granted_by_user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        _ts("granted_at", nullable=False, server_default=sa.func.now()),
        _ts("expires_at", nullable=False),
        _ts("revoked_at"),
        sa.Column("revoked_by_user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.CheckConstraint("scope IN ('supplier_quotes')", name="ck_ai_sharing_authorizations_scope"),
    )
    op.create_table(
        "company_registration",
        sa.Column("uei", sa.Text(), primary_key=True),
        sa.Column("legal_name", sa.Text()),
        sa.Column("cage_code", sa.Text()),
        sa.Column("registration_status", sa.Text()),
        sa.Column("expiration_date", sa.Date()),
        sa.Column("source", sa.Text(), nullable=False, server_default="sam_entity_api"),
        _ts("refreshed_at", nullable=False),
        sa.Column("raw", postgresql.JSONB()),
        _ts("last_expiry_alert_at"),
        *_stamps(),
    )
    op.drop_constraint("ck_tasks_task_type", "tasks")
    op.create_check_constraint("ck_tasks_task_type", "tasks", f"task_type IN {_NEW_TASKS}")
    for table in _TABLES_WITH_UPDATED_AT:
        op.execute(f"CREATE TRIGGER trg_{table}_set_updated_at BEFORE UPDATE ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION govcon_set_updated_at()")


def downgrade():
    op.execute("DELETE FROM tasks WHERE task_type = 'quote_extraction'")
    op.drop_constraint("ck_tasks_task_type", "tasks")
    op.create_check_constraint("ck_tasks_task_type", "tasks", f"task_type IN {_OLD_TASKS}")
    for table in _TABLES_WITH_UPDATED_AT:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_set_updated_at ON {table}")
    for table in ("company_registration", "ai_sharing_authorizations", "rfq_drafts", "supplier_quote_lines",
                  "supplier_quotes", "supplier_products", "catalog_imports", "products", "suppliers"):
        op.drop_table(table)
