"""Add multicore concurrency fields and podcast_tts_chunks table

Revision ID: 009_multicore_concurrency
Revises: 008_phase2_improvements
Create Date: 2026-08-10 23:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = '009_multicore_concurrency'
down_revision: Union[str, None] = '008_phase2_improvements'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add claim/lease columns to podcast_jobs
    with op.batch_alter_table('podcast_jobs') as batch_op:
        batch_op.add_column(sa.Column('claimed_by', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True))

    # 2. Create podcast_tts_chunks table
    op.create_table(
        'podcast_tts_chunks',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('job_id', sa.String(length=36), nullable=False),
        sa.Column('chunk_index', sa.Integer(), nullable=False),
        sa.Column('text_hash', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=50), nullable=False, server_default='PENDING'),
        sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('local_path', sa.String(length=512), nullable=True),
        sa.Column('audio_duration', sa.Float(), nullable=True),
        sa.Column('claimed_by', sa.String(length=100), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('error_detail', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['job_id'], ['podcast_jobs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('job_id', 'chunk_index', name='uq_podcast_tts_chunks_job_index'),
    )
    op.create_index('idx_podcast_tts_chunks_job_id', 'podcast_tts_chunks', ['job_id'])
    op.create_index('idx_tts_chunks_job_status', 'podcast_tts_chunks', ['job_id', 'status'])


def downgrade() -> None:
    op.drop_index('idx_tts_chunks_job_status', table_name='podcast_tts_chunks')
    op.drop_index('idx_podcast_tts_chunks_job_id', table_name='podcast_tts_chunks')
    op.drop_table('podcast_tts_chunks')

    with op.batch_alter_table('podcast_jobs') as batch_op:
        batch_op.drop_column('heartbeat_at')
        batch_op.drop_column('lease_expires_at')
        batch_op.drop_column('claimed_by')
