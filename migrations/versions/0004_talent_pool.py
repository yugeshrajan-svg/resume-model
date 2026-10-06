"""Talent pool: resume vectors for semantic search across all of a company's candidates.

Vectors come from the redacted resume text (contact details removed) and cascade with the resume and candidate,
so erasure and retention remove them. Stored as REAL[] so no database extension is required; the search code
can move to pgvector where it is available.

Revision ID: 0004_talent_pool
Revises: 0003_hr_management
"""

from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op

revision = "0004_talent_pool"
down_revision = "0003_hr_management"
branch_labels = None
depends_on = None

APP_ROLE = os.environ.get("APP_DB_ROLE", "screening_app")


def upgrade() -> None:
    op.create_table(
        "candidate_embeddings",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("company_id", sa.UUID(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("candidate_id", sa.UUID(), sa.ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("resume_id", sa.UUID(), sa.ForeignKey("resumes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("vector", sa.ARRAY(sa.REAL()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("resume_id", "model"),
    )
    op.create_index("ix_candidate_embeddings_search", "candidate_embeddings", ["company_id", "model"])
    op.create_index(op.f("ix_candidate_embeddings_candidate_id"), "candidate_embeddings", ["candidate_id"])
    op.execute("ALTER TABLE candidate_embeddings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE candidate_embeddings FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON candidate_embeddings USING (company_id = app_current_company()) "
        "WITH CHECK (company_id = app_current_company())"
    )
    op.execute(f"GRANT SELECT, INSERT, DELETE ON candidate_embeddings TO {APP_ROLE}")


def downgrade() -> None:
    op.drop_table("candidate_embeddings")
