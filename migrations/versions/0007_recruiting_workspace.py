"""Hiring pipeline, interviews, feedback, tasks and offers with tenant RLS."""

import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_recruiting_workspace"
down_revision = "0006_queue_priority"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_applications_stage", "applications", type_="check")
    op.create_check_constraint(
        "ck_applications_stage",
        "applications",
        "stage IN ('applied','screened','review','shortlisted','interview','offer','hired','rejected','withdrawn')",
    )
    op.drop_constraint("ck_decisions_decision", "decisions", type_="check")
    op.create_check_constraint(
        "ck_decisions_decision", "decisions", "decision IN ('shortlist','reject','review','move')"
    )
    op.create_table(
        "recruiting_activities",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("company_id", sa.UUID(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("candidate_id", sa.UUID(), sa.ForeignKey("candidates.id", ondelete="CASCADE")),
        sa.Column("job_id", sa.UUID(), sa.ForeignKey("jobs.id", ondelete="CASCADE")),
        sa.Column("application_id", sa.UUID(), sa.ForeignKey("applications.id", ondelete="CASCADE")),
        sa.Column("created_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("assigned_to", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), server_default="open", nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True)),
        sa.Column("end_at", sa.DateTime(timezone=True)),
        sa.Column("details", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("kind IN ('task','interview','feedback','note','offer')", name="ck_recruiting_kind"),
        sa.CheckConstraint(
            "status IN ('open','completed','cancelled','accepted','declined')", name="ck_recruiting_status"
        ),
    )
    op.create_index("ix_recruiting_company_due", "recruiting_activities", ["company_id", "due_at"])
    for column in ("candidate_id", "job_id"):
        op.create_index(f"ix_recruiting_activities_{column}", "recruiting_activities", [column])
    op.execute("ALTER TABLE recruiting_activities ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE recruiting_activities FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON recruiting_activities USING (company_id = app_current_company()) "
        "WITH CHECK (company_id = app_current_company())"
    )
    role = os.environ.get("APP_DB_ROLE", "screening_app")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON recruiting_activities TO {role}")


def downgrade():
    # A downgrade must not silently throw away new hiring stages or history.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM applications WHERE stage IN ('interview','offer','hired','withdrawn')) "
        "OR EXISTS (SELECT 1 FROM decisions WHERE decision='move') THEN RAISE EXCEPTION "
        "'New hiring stages/history exist; downgrade cannot preserve them'; END IF; END $$"
    )
    op.drop_table("recruiting_activities")
    op.drop_constraint("ck_applications_stage", "applications", type_="check")
    op.create_check_constraint(
        "ck_applications_stage", "applications", "stage IN ('applied','screened','review','shortlisted','rejected')"
    )
    op.drop_constraint("ck_decisions_decision", "decisions", type_="check")
    op.create_check_constraint("ck_decisions_decision", "decisions", "decision IN ('shortlist','reject','review')")
