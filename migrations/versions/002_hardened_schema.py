"""Hardened schema migration for operational fields and delivery state machine

Revision ID: 002_hardened_schema
Revises: 001_initial_schema
Create Date: 2026-08-06 14:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = '002_hardened_schema'
down_revision: Union[str, None] = '001_initial_schema'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add new operational fields to podcast_jobs
    op.add_column('podcast_jobs', sa.Column('custom_voice', sa.String(length=50), nullable=True))
    op.add_column('podcast_jobs', sa.Column('custom_speed', sa.Float(), nullable=True))
    op.add_column('podcast_jobs', sa.Column('custom_title', sa.String(length=255), nullable=True))
    
    op.add_column('podcast_jobs', sa.Column('synthesis_attempt_count', sa.Integer(), server_default='0', nullable=False))
    op.add_column('podcast_jobs', sa.Column('delivery_attempt_count', sa.Integer(), server_default='0', nullable=False))
    op.add_column('podcast_jobs', sa.Column('failed_stage', sa.String(length=50), nullable=True))
    op.add_column('podcast_jobs', sa.Column('next_retry_at', sa.DateTime(timezone=True), nullable=True))
    
    op.add_column('podcast_jobs', sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('podcast_jobs', sa.Column('claim_owner', sa.String(length=100), nullable=True))
    op.add_column('podcast_jobs', sa.Column('last_heartbeat_at', sa.DateTime(timezone=True), nullable=True))
    
    op.add_column('podcast_jobs', sa.Column('drive_uploaded_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('podcast_jobs', sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True))

    op.create_index('idx_podcast_jobs_claim', 'podcast_jobs', ['status', 'claimed_at'])


def downgrade() -> None:
    op.drop_index('idx_podcast_jobs_claim', table_name='podcast_jobs')
    
    op.drop_column('podcast_jobs', 'delivered_at')
    op.drop_column('podcast_jobs', 'drive_uploaded_at')
    op.drop_column('podcast_jobs', 'last_heartbeat_at')
    op.drop_column('podcast_jobs', 'claim_owner')
    op.drop_column('podcast_jobs', 'claimed_at')
    op.drop_column('podcast_jobs', 'next_retry_at')
    op.drop_column('podcast_jobs', 'failed_stage')
    op.drop_column('podcast_jobs', 'delivery_attempt_count')
    op.drop_column('podcast_jobs', 'synthesis_attempt_count')
    op.drop_column('podcast_jobs', 'custom_title')
    op.drop_column('podcast_jobs', 'custom_speed')
    op.drop_column('podcast_jobs', 'custom_voice')
