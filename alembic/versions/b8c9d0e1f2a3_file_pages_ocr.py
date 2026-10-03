"""Page-level text and OCR tracking for stored files (ADR-065).

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-10-03
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "b8c9d0e1f2a3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("files", sa.Column("page_count", sa.Integer()))
    op.add_column("files", sa.Column("ocr_pages", postgresql.JSONB()))
    op.add_column("files", sa.Column("ocr_failed_pages", postgresql.JSONB()))
    op.create_table(
        "file_pages",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("file_id", sa.BigInteger(), sa.ForeignKey("files.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.Column("label", sa.Text()),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("text_source", sa.Text(), nullable=False),
        sa.Column("ocr_confidence", sa.Numeric()),
        sa.Column("char_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("file_id", "page_no", name="uq_file_pages_file_page"),
        sa.CheckConstraint("text_source IN ('native','ocr','none')", name="ck_file_pages_text_source"),
    )


def downgrade():
    op.drop_table("file_pages")
    op.drop_column("files", "ocr_failed_pages")
    op.drop_column("files", "ocr_pages")
    op.drop_column("files", "page_count")
