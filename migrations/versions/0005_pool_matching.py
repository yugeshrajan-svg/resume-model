"""Pool matching: talent-pool consent scope and per-version eligibility of pool candidates.

* consents.scope: "job" (existing grants) or "talent_pool" (also matching against future roles).
* pool_matches: eligibility of each consenting pool candidate for a confirmed requirements version, computed with
  the screening extraction and knockout rules. Suggestions only; cascades with the candidate, resume and job.

Revision ID: 0005_pool_matching
Revises: 0004_talent_pool
"""

from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_pool_matching"
down_revision = "0004_talent_pool"
branch_labels = None
depends_on = None

APP_ROLE = os.environ.get("APP_DB_ROLE", "screening_app")


def upgrade() -> None:
    op.add_column("consents", sa.Column("scope", sa.String(20), server_default="job", nullable=False))
    op.create_check_constraint("ck_consents_scope", "consents", "scope IN ('job', 'talent_pool')")

    op.create_table(
        "pool_matches",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("company_id", sa.UUID(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", sa.UUID(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "requirements_id", sa.UUID(), sa.ForeignKey("job_requirements.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("requirements_version", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.UUID(), sa.ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("resume_id", sa.UUID(), sa.ForeignKey("resumes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(10), server_default="pending", nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=True),
        sa.Column("knockout", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("similarity", sa.Float(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("requirements_id", "candidate_id"),
        sa.CheckConstraint("status IN ('pending', 'checked', 'failed')", name="ck_pool_matches_status"),
    )
    op.create_index(op.f("ix_pool_matches_company_id"), "pool_matches", ["company_id"])
    op.create_index(op.f("ix_pool_matches_job_id"), "pool_matches", ["job_id"])
    op.create_index(op.f("ix_pool_matches_candidate_id"), "pool_matches", ["candidate_id"])
    op.execute("ALTER TABLE pool_matches ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE pool_matches FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON pool_matches USING (company_id = app_current_company()) "
        "WITH CHECK (company_id = app_current_company())"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON pool_matches TO {APP_ROLE}")


def downgrade() -> None:
    op.drop_table("pool_matches")
    op.drop_constraint("ck_consents_scope", "consents", type_="check")
    op.drop_column("consents", "scope")
