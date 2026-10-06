"""Frozen screening inputs and durable upload associations.

Revision ID: 0002_durable_workflow
Revises: b5ae556e273f
"""

from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_durable_workflow"
down_revision = "b5ae556e273f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("resumes", sa.Column("processing_generation", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("resumes", sa.Column("processed_generation", sa.Integer(), nullable=False, server_default="0"))
    op.execute("UPDATE resumes SET processed_generation = 1 WHERE candidate_id IS NOT NULL")
    op.add_column("screening_runs", sa.Column("snapshot", JSONB(), nullable=True))
    op.add_column("screening_runs", sa.Column("reference_date", sa.Date(), nullable=True))
    op.add_column("screening_runs", sa.Column("retry_of_run_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_run_retry", "screening_runs", "screening_runs", ["retry_of_run_id"], ["id"], ondelete="SET NULL"
    )
    op.create_table(
        "resume_registrations",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("company_id", sa.UUID(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("resume_id", sa.UUID(), sa.ForeignKey("resumes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", sa.UUID(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=True),
        sa.Column("consent", JSONB(), nullable=True),
        sa.Column("blob_names", JSONB(), nullable=False, server_default="[]"),
        sa.Column("recorded_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("resume_id", "job_id"),
    )
    op.create_table(
        "screening_inputs",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("company_id", sa.UUID(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", sa.UUID(), sa.ForeignKey("screening_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("application_id", sa.UUID(), sa.ForeignKey("applications.id", ondelete="CASCADE"), nullable=False),
        sa.Column("candidate_id", sa.UUID(), sa.ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("resume_id", sa.UUID(), sa.ForeignKey("resumes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("resume_version", sa.Integer(), nullable=False),
        sa.Column("processing_generation", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("redacted_text", sa.Text(), nullable=False),
        sa.Column("extraction", JSONB(), nullable=True),
        sa.UniqueConstraint("run_id", "application_id"),
    )
    for table, columns in (
        ("resume_registrations", ("company_id", "resume_id")),
        ("screening_inputs", ("company_id", "run_id", "candidate_id")),
    ):
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} USING (company_id = app_current_company()) "
            "WITH CHECK (company_id = app_current_company())"
        )
        role = os.environ.get("APP_DB_ROLE", "screening_app")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role}")
        if table == "screening_inputs":
            op.execute(f"REVOKE UPDATE ON {table} FROM {role}")
            op.execute(f"GRANT UPDATE (extraction) ON {table} TO {role}")
    # Preserve pre-upgrade pending uploads. Existing runs cannot be truthfully snapshotted retroactively.
    op.execute("""INSERT INTO resume_registrations (id, company_id, resume_id, job_id, consent, blob_names, applied_at)
                  SELECT id, company_id, id, pending_job_id, pending_consent,
                         jsonb_build_array(blob_name),
                         CASE WHEN candidate_id IS NOT NULL THEN created_at ELSE NULL END
                  FROM resumes WHERE pending_job_id IS NOT NULL OR pending_consent IS NOT NULL""")
    op.execute("UPDATE resumes SET pending_job_id = NULL, pending_consent = NULL")


def downgrade() -> None:
    op.execute("""UPDATE resumes r SET pending_job_id = x.job_id, pending_consent = x.consent
                  FROM (SELECT DISTINCT ON (resume_id) resume_id, job_id, consent
                        FROM resume_registrations WHERE applied_at IS NULL ORDER BY resume_id, created_at) x
                  WHERE r.id = x.resume_id""")
    op.drop_table("screening_inputs")
    op.drop_table("resume_registrations")
    op.drop_constraint("fk_run_retry", "screening_runs", type_="foreignkey")
    for column in ("retry_of_run_id", "reference_date", "snapshot"):
        op.drop_column("screening_runs", column)
    for column in ("processed_generation", "processing_generation"):
        op.drop_column("resumes", column)
