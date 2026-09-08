"""Add rerun lineage, generation settings, progress notification claim, and auto diagnostics

Revision ID: 015_rerun_lineage_diagnostics
Revises: 014_diag_events
Create Date: 2026-09-08 17:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '015_rerun_lineage_diagnostics'
down_revision: Union[str, None] = '014_diag_events'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('podcast_jobs') as batch_op:
        batch_op.add_column(
            sa.Column('rerun_of_job_id', sa.String(length=36), nullable=True)
        )
        batch_op.add_column(
            sa.Column('generation_settings_json', sa.JSON(), nullable=True)
        )
        batch_op.add_column(
            sa.Column('first_chunk_progress_claimed_at', sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column('telegram_progress_message_id', sa.BigInteger(), nullable=True)
        )
        batch_op.add_column(
            sa.Column('auto_diagnostics_json', sa.JSON(), nullable=True)
        )
        batch_op.create_foreign_key(
            'fk_podcast_jobs_rerun_of_job_id',
            'podcast_jobs',
            ['rerun_of_job_id'],
            ['id'],
            ondelete='SET NULL',
        )
        batch_op.create_index(
            'idx_podcast_jobs_rerun_of_job_id',
            ['rerun_of_job_id'],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table('podcast_jobs') as batch_op:
        batch_op.drop_index('idx_podcast_jobs_rerun_of_job_id')
        batch_op.drop_constraint('fk_podcast_jobs_rerun_of_job_id', type_='foreignkey')
        batch_op.drop_column('auto_diagnostics_json')
        batch_op.drop_column('telegram_progress_message_id')
        batch_op.drop_column('first_chunk_progress_claimed_at')
        batch_op.drop_column('generation_settings_json')
        batch_op.drop_column('rerun_of_job_id')
