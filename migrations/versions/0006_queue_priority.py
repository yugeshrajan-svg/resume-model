"""Queue priority: interactive jobs run before bulk background work.

Talent-pool indexing and matching can queue thousands of jobs; without a priority, new uploads and screening runs
would wait behind them in the first-in, first-out queue.

Revision ID: 0006_queue_priority
Revises: 0005_pool_matching
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_queue_priority"
down_revision = "0005_pool_matching"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("queue_jobs", sa.Column("priority", sa.SmallInteger(), server_default="0", nullable=False))
    op.execute("UPDATE queue_jobs SET priority = 1 WHERE kind = 'retention_sweep'")
    op.execute("UPDATE queue_jobs SET priority = 2 WHERE kind IN ('embed_resume', 'match_pool', 'pool_check')")
    op.drop_index("ix_queue_claim", table_name="queue_jobs")
    op.create_index("ix_queue_claim", "queue_jobs", ["status", "priority", "run_after"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_queue_claim", table_name="queue_jobs")
    op.create_index("ix_queue_claim", "queue_jobs", ["status", "run_after"], unique=False)
    op.drop_column("queue_jobs", "priority")
