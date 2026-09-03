"""Add source and diagnostics drive artifact fields, audio_ready_at, voice, speed, model

Revision ID: 005_drive_artifacts
Revises: 004_align_orm_schema
Create Date: 2026-08-07 11:30:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "005_drive_artifacts"
down_revision: Union[str, None] = "004_align_orm_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("podcast_jobs") as batch_op:
        batch_op.add_column(sa.Column("source_drive_file_id", sa.String(length=255), nullable=True))
        batch_op.add_column(
            sa.Column("source_drive_web_link", sa.String(length=512), nullable=True)
        )
        batch_op.add_column(
            sa.Column("diagnostics_drive_file_id", sa.String(length=255), nullable=True)
        )
        batch_op.add_column(
            sa.Column("diagnostics_drive_web_link", sa.String(length=512), nullable=True)
        )
        batch_op.add_column(sa.Column("audio_ready_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("kokoro_voice", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("kokoro_speed", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("gemini_model", sa.String(length=50), nullable=True))

        batch_op.create_index(
            "idx_podcast_jobs_source_drive_file_id", ["source_drive_file_id"], unique=True
        )
        batch_op.create_index(
            "idx_podcast_jobs_diagnostics_drive_file_id", ["diagnostics_drive_file_id"], unique=True
        )


def downgrade() -> None:
    with op.batch_alter_table("podcast_jobs") as batch_op:
        batch_op.drop_index("idx_podcast_jobs_diagnostics_drive_file_id")
        batch_op.drop_index("idx_podcast_jobs_source_drive_file_id")
        batch_op.drop_column("gemini_model")
        batch_op.drop_column("kokoro_speed")
        batch_op.drop_column("kokoro_voice")
        batch_op.drop_column("audio_ready_at")
        batch_op.drop_column("diagnostics_drive_web_link")
        batch_op.drop_column("diagnostics_drive_file_id")
        batch_op.drop_column("source_drive_web_link")
        batch_op.drop_column("source_drive_file_id")
