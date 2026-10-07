"""Permit candidate reference changes while retaining frozen screening evidence."""

import os

from alembic import op

revision = "0011_consolidation_references"
down_revision = "0010_candidate_aliases"
branch_labels = None
depends_on = None


def upgrade():
    # The frozen resume version, run, application, hash and evidence remain read-only.
    # Tenant RLS still applies to a consolidation's candidate reference change.
    role = os.environ.get("APP_DB_ROLE", "screening_app")
    for table in ("screening_inputs", "candidate_embeddings"):
        op.execute(f"GRANT UPDATE (candidate_id) ON {table} TO {role}")


def downgrade():
    role = os.environ.get("APP_DB_ROLE", "screening_app")
    for table in ("screening_inputs", "candidate_embeddings"):
        op.execute(f"REVOKE UPDATE (candidate_id) ON {table} FROM {role}")
