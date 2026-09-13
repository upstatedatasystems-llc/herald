"""Migration 019: Long-form telemetry and crash recovery enhancements.

Adds research_provider and research_model columns to podcast_jobs table.

Revision ID: 019_longform_fixes_recovery
Revises: 018_interactive_params_longform
Create Date: 2026-09-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "019_longform_fixes_recovery"
down_revision: str | None = "018_interactive_params_longform"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("podcast_jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("research_provider", sa.String(length=50), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("podcast_jobs", schema=None) as batch_op:
        batch_op.drop_column("research_provider")
