"""Add performance metrics table and unified details drive artifact fields

Revision ID: 007_performance_and_details
Revises: 006_research_mode_fields
Create Date: 2026-08-08 21:45:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = '007_performance_and_details'
down_revision: Union[str, None] = '006_research_mode_fields'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create job_processing_metrics table
    op.create_table(
        'job_processing_metrics',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('job_id', sa.String(length=36), sa.ForeignKey('podcast_jobs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('stage', sa.String(length=50), nullable=False),
        sa.Column('substage', sa.String(length=50), nullable=True),
        sa.Column('attempt', sa.Integer(), nullable=True),
        sa.Column('sequence_index', sa.Integer(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('duration_ms', sa.BigInteger(), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('input_chars', sa.Integer(), nullable=True),
        sa.Column('output_bytes', sa.BigInteger(), nullable=True),
        sa.Column('audio_duration_ms', sa.BigInteger(), nullable=True),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )

    op.create_index('idx_job_processing_metrics_job_stage', 'job_processing_metrics', ['job_id', 'stage'])
    op.create_index('idx_job_processing_metrics_stage_created', 'job_processing_metrics', ['stage', 'created_at'])
    op.create_index('idx_job_processing_metrics_job_seq', 'job_processing_metrics', ['job_id', 'sequence_index'])

    # 2. Add columns to podcast_jobs table
    with op.batch_alter_table('podcast_jobs') as batch_op:
        batch_op.add_column(sa.Column('gmail_received_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('details_drive_file_id', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('details_drive_web_link', sa.String(length=512), nullable=True))
        batch_op.create_index('idx_podcast_jobs_details_drive_file_id', ['details_drive_file_id'], unique=True)


def downgrade() -> None:
    with op.batch_alter_table('podcast_jobs') as batch_op:
        batch_op.drop_index('idx_podcast_jobs_details_drive_file_id')
        batch_op.drop_column('details_drive_web_link')
        batch_op.drop_column('details_drive_file_id')
        batch_op.drop_column('gmail_received_at')

    op.drop_index('idx_job_processing_metrics_job_seq', table_name='job_processing_metrics')
    op.drop_index('idx_job_processing_metrics_stage_created', table_name='job_processing_metrics')
    op.drop_index('idx_job_processing_metrics_job_stage', table_name='job_processing_metrics')
    op.drop_table('job_processing_metrics')
