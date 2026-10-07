"""Preserve hashed deduplication identities during explicit profile consolidation."""

import os
import sqlalchemy as sa
from alembic import op

revision = "0010_candidate_aliases"
down_revision = "0009_application_sources"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "candidate_aliases",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("company_id", sa.UUID(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("candidate_id", sa.UUID(), sa.ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("contact_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("company_id", "kind", "contact_hash"),
    )
    op.create_index("ix_candidate_aliases_company_id", "candidate_aliases", ["company_id"])
    op.create_index("ix_candidate_aliases_candidate_id", "candidate_aliases", ["candidate_id"])
    op.execute("ALTER TABLE candidate_aliases ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE candidate_aliases FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON candidate_aliases USING (company_id = app_current_company()) "
        "WITH CHECK (company_id = app_current_company())"
    )
    role = os.environ.get("APP_DB_ROLE", "screening_app")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON candidate_aliases TO {role}")


def downgrade():
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM candidate_aliases) THEN RAISE EXCEPTION "
        "'Identity aliases exist; refusing data-losing downgrade'; END IF; END $$"
    )
    op.drop_table("candidate_aliases")
