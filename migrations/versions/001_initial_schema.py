"""Initial schema migration for Herald

Revision ID: 001_initial_schema
Revises: 
Create Date: 2026-08-06 13:31:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = '001_initial_schema'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'podcast_jobs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('gmail_message_id', sa.String(length=255), nullable=False),
        sa.Column('gmail_thread_id', sa.String(length=255), nullable=True),
        sa.Column('sender_email', sa.String(length=255), nullable=False),
        sa.Column('request_mode', sa.String(length=50), nullable=False, server_default='STANDARD'),
        sa.Column('source_type', sa.String(length=50), nullable=False, server_default='EMAIL_BODY'),
        sa.Column('source_url', sa.Text(), nullable=True),
        sa.Column('source_hash', sa.String(length=64), nullable=False),
        sa.Column('source_text', sa.Text(), nullable=False),
        sa.Column('script_json', sa.JSON(), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=False, server_default='RECEIVED'),
        sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('completed_chunk_index', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('local_audio_path', sa.Text(), nullable=True),
        sa.Column('audio_sha256', sa.String(length=64), nullable=True),
        sa.Column('audio_bytes', sa.BigInteger(), nullable=True),
        sa.Column('audio_duration_seconds', sa.Integer(), nullable=True),
        sa.Column('drive_file_id', sa.String(length=255), nullable=True),
        sa.Column('drive_web_link', sa.Text(), nullable=True),
        sa.Column('error_code', sa.String(length=100), nullable=True),
        sa.Column('error_detail', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('gmail_message_id'),
        sa.UniqueConstraint('drive_file_id')
    )
    op.create_index('idx_podcast_jobs_status', 'podcast_jobs', ['status'])
    op.create_index('idx_podcast_jobs_sender', 'podcast_jobs', ['sender_email'])
    op.create_index('idx_podcast_jobs_source_hash', 'podcast_jobs', ['source_hash'])
    op.create_index('idx_podcast_jobs_status_created', 'podcast_jobs', ['status', 'created_at'])

    op.create_table(
        'job_state_transitions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('job_id', sa.String(length=36), nullable=False),
        sa.Column('from_state', sa.String(length=50), nullable=True),
        sa.Column('to_state', sa.String(length=50), nullable=False),
        sa.Column('component', sa.String(length=100), nullable=False),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('error_category', sa.String(length=100), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['job_id'], ['podcast_jobs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_job_state_transitions_job_id', 'job_state_transitions', ['job_id'])


def downgrade() -> None:
    op.drop_index('idx_job_state_transitions_job_id', table_name='job_state_transitions')
    op.drop_table('job_state_transitions')

    op.drop_index('idx_podcast_jobs_status_created', table_name='podcast_jobs')
    op.drop_index('idx_podcast_jobs_source_hash', table_name='podcast_jobs')
    op.drop_index('idx_podcast_jobs_sender', table_name='podcast_jobs')
    op.drop_index('idx_podcast_jobs_status', table_name='podcast_jobs')
    op.drop_table('podcast_jobs')
