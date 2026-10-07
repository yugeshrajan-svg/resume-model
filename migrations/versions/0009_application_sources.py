"""Record new application sources without inventing historical attribution."""

import sqlalchemy as sa
from alembic import op

revision = "0009_application_sources"
down_revision = "0008_hr_workflows"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("applications", "resume_registrations"):
        op.add_column(table, sa.Column("source_type", sa.String(30), nullable=False, server_default="not_recorded"))


def downgrade():
    for table in ("applications", "resume_registrations"):
        op.drop_column(table, "source_type")
