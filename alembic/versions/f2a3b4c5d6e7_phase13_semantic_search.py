"""Phase 13 — semantic search: watchlist embeddings + HNSW vector index

Revision ID: f2a3b4c5d6e7
Revises: a1b2c3d4e5f6
Create Date: 2026-09-27 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "f2a3b4c5d6e7"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add watchlist profile embedding columns (Phase 13 task 3)
    op.add_column("watchlists", sa.Column("embedding", Vector(384), nullable=True))
    op.add_column(
        "watchlists",
        sa.Column(
            "embedding_updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    # HNSW approximate-nearest-neighbor index on opportunities.embedding.
    # cosine distance is appropriate for normalised sentence-transformer outputs.
    # m=16 / ef_construction=64 are standard defaults; searchable at target scale.
    # Non-CONCURRENTLY because CONCURRENTLY cannot run inside a transaction block
    # (which is how Alembic runs migrations).  For zero-downtime production upgrades,
    # build the index manually with CONCURRENTLY before running migrations.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_opportunities_embedding_hnsw
        ON opportunities
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        """
    )

    # HNSW index on watchlist profile embeddings.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_watchlists_embedding_hnsw
        ON watchlists
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_watchlists_embedding_hnsw")
    op.execute("DROP INDEX IF EXISTS ix_opportunities_embedding_hnsw")
    op.drop_column("watchlists", "embedding_updated_at")
    op.drop_column("watchlists", "embedding")
