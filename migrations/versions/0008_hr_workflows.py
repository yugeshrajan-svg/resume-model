"""Versioned interview plans and hiring workflow records."""

import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008_hr_workflows"
down_revision = "0007_recruiting_workspace"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_users_role", "users", type_="check")
    op.create_check_constraint("ck_users_role", "users", "role IN ('admin','recruiter','hiring_manager','employee')")
    op.create_table(
        "hiring_plans",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("company_id", sa.UUID(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", sa.UUID(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("rounds", JSONB(), nullable=False),
        sa.Column("created_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("job_id", "version"),
    )
    op.create_table(
        "hr_records",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("company_id", sa.UUID(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("data", JSONB(), nullable=False),
        sa.Column("history", JSONB(), server_default="[]", nullable=False),
        sa.Column("candidate_id", sa.UUID(), sa.ForeignKey("candidates.id", ondelete="CASCADE")),
        sa.Column("job_id", sa.UUID(), sa.ForeignKey("jobs.id", ondelete="CASCADE")),
        sa.Column("activity_id", sa.UUID(), sa.ForeignKey("recruiting_activities.id", ondelete="CASCADE")),
        sa.Column("parent_id", sa.UUID(), sa.ForeignKey("hr_records.id", ondelete="CASCADE")),
        sa.Column("subject_user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("assigned_to", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_hiring_plans_company_id", "hiring_plans", ["company_id"])
    op.create_index("ix_hiring_plans_job_id", "hiring_plans", ["job_id"])
    for column in ("company_id", "candidate_id", "parent_id", "subject_user_id"):
        op.create_index(f"ix_hr_records_{column}", "hr_records", [column])
    op.create_index("ix_hr_company_kind", "hr_records", ["company_id", "kind"])
    for table in ("hiring_plans", "hr_records"):
        op.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        op.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        op.execute(
            text(
                f"CREATE POLICY tenant_isolation ON {table} USING (company_id = app_current_company()) "
                "WITH CHECK (company_id = app_current_company())"
            )
        )
        role = os.environ.get("APP_DB_ROLE", "screening_app")
        op.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role}"))


def downgrade():
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM users WHERE role='employee') OR EXISTS (SELECT 1 FROM hr_records) "
        "OR EXISTS (SELECT 1 FROM hiring_plans) THEN RAISE EXCEPTION "
        "'HR data exists; refusing data-losing downgrade'; END IF; END $$"
    )
    op.drop_table("hr_records")
    op.drop_table("hiring_plans")
    op.drop_constraint("ck_users_role", "users", type_="check")
    op.create_check_constraint("ck_users_role", "users", "role IN ('admin','recruiter','hiring_manager')")
