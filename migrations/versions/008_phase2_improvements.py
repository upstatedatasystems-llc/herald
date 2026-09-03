"""Add Phase 2 improvements fields to podcast_jobs

Revision ID: 008_phase2_improvements
Revises: 007_performance_and_details
Create Date: 2026-08-09 23:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "008_phase2_improvements"
down_revision: Union[str, None] = "007_performance_and_details"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("podcast_jobs") as batch_op:
        batch_op.add_column(
            sa.Column("tts_chunk_chars", sa.Integer(), nullable=True, server_default="500")
        )
        batch_op.add_column(
            sa.Column("verify_final_script", sa.Boolean(), nullable=True, server_default="false")
        )
        batch_op.add_column(sa.Column("verify_audit_json", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column("verify_repair_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("tts_resource_metrics_json", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column("details_finalized_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("podcast_jobs") as batch_op:
        batch_op.drop_column("details_finalized_at")
        batch_op.drop_column("tts_resource_metrics_json")
        batch_op.drop_column("verify_repair_count")
        batch_op.drop_column("verify_audit_json")
        batch_op.drop_column("verify_final_script")
        batch_op.drop_column("tts_chunk_chars")
