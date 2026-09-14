"""Migration 020: Add research degradation tracking fields to podcast_jobs.

Revision ID: 020_research_degraded
Revises: 019_longform_fixes_recovery
Create Date: 2026-09-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "020_research_degraded"
down_revision: str | None = "019_longform_fixes_recovery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("podcast_jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("research_degraded", sa.Boolean(), nullable=True, server_default=sa.false()))
        batch_op.add_column(sa.Column("research_degradation_reason", sa.String(length=100), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("podcast_jobs", schema=None) as batch_op:
        batch_op.drop_column("research_degradation_reason")
        batch_op.drop_column("research_degraded")
