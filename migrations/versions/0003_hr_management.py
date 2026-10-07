"""HR management: decision history and erasure tombstones.

* decisions gain previous_stage/new_stage and become append-only for the app role (history is immutable;
  candidate erasure still removes them through the foreign-key cascade).
* erasure_tombstones keep keyed contact hashes (HMAC, never raw contact details) of erased candidates for a
  limited time, so uploads registered before an erasure cannot recreate the erased person when their
  ingestion job runs later.

Revision ID: 0003_hr_management
Revises: 0002_durable_workflow
"""

from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op

revision = "0003_hr_management"
down_revision = "0002_durable_workflow"
branch_labels = None
depends_on = None

APP_ROLE = os.environ.get("APP_DB_ROLE", "screening_app")


def upgrade() -> None:
    op.add_column("decisions", sa.Column("previous_stage", sa.String(20), nullable=True))
    op.add_column("decisions", sa.Column("new_stage", sa.String(20), nullable=True))
    op.execute(
        """UPDATE decisions SET new_stage = CASE decision WHEN 'shortlist' THEN 'shortlisted'
                                                         WHEN 'reject' THEN 'rejected' ELSE 'review' END"""
    )
    op.create_index("ix_decisions_application_created", "decisions", ["application_id", "created_at"])
    op.execute(f"REVOKE UPDATE, DELETE ON decisions FROM {APP_ROLE}")

    op.create_table(
        "erasure_tombstones",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("company_id", sa.UUID(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("contact_hash", sa.String(64), nullable=False),
        sa.Column("erased_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('email', 'phone')", name="ck_erasure_tombstones_kind"),
    )
    op.create_index("ix_erasure_tombstones_lookup", "erasure_tombstones", ["company_id", "contact_hash"])
    op.execute("ALTER TABLE erasure_tombstones ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE erasure_tombstones FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON erasure_tombstones USING (company_id = app_current_company()) "
        "WITH CHECK (company_id = app_current_company())"
    )
    op.execute(f"GRANT SELECT, INSERT, DELETE ON erasure_tombstones TO {APP_ROLE}")


def downgrade() -> None:
    op.drop_table("erasure_tombstones")
    op.execute(f"GRANT UPDATE, DELETE ON decisions TO {APP_ROLE}")
    op.drop_index("ix_decisions_application_created", table_name="decisions")
    op.drop_column("decisions", "new_stage")
    op.drop_column("decisions", "previous_stage")
