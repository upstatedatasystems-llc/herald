"""Add job_diagnostic_events table and extend ai_interactions

Revision ID: 014_diag_events
Revises: 013_ai_interactions
Create Date: 2026-09-03 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '014_diag_events'
down_revision: Union[str, None] = '013_ai_interactions'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create job_diagnostic_events table
    op.create_table(
        'job_diagnostic_events',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('job_id', sa.String(length=36), nullable=False),
        sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('level', sa.String(length=16), nullable=False, server_default='INFO'),
        sa.Column('component', sa.String(length=32), nullable=False),
        sa.Column('event_type', sa.String(length=64), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('metadata_json_sanitized', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.ForeignKeyConstraint(['job_id'], ['podcast_jobs.id'], name='fk_diag_events_job_id', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name='pk_job_diagnostic_events'),
    )
    op.create_index('idx_diag_events_job_time', 'job_diagnostic_events', ['job_id', 'timestamp'], unique=False)
    op.create_index('idx_diag_events_comp_type', 'job_diagnostic_events', ['component', 'event_type'], unique=False)

    # 2. Extend ai_interactions table with explicit evidence fields
    op.add_column('ai_interactions', sa.Column('attempt', sa.Integer(), nullable=True, server_default='1'))
    op.add_column('ai_interactions', sa.Column('http_status', sa.Integer(), nullable=True))
    op.add_column('ai_interactions', sa.Column('provider_request_id', sa.String(length=128), nullable=True))
    op.add_column('ai_interactions', sa.Column('input_chars', sa.Integer(), nullable=True))
    op.add_column('ai_interactions', sa.Column('request_json_sanitized', sa.JSON(), nullable=True))
    op.add_column('ai_interactions', sa.Column('response_json_sanitized', sa.JSON(), nullable=True))


def downgrade() -> None:
    # 1. Drop added columns on ai_interactions
    op.drop_column('ai_interactions', 'response_json_sanitized')
    op.drop_column('ai_interactions', 'request_json_sanitized')
    op.drop_column('ai_interactions', 'input_chars')
    op.drop_column('ai_interactions', 'provider_request_id')
    op.drop_column('ai_interactions', 'http_status')
    op.drop_column('ai_interactions', 'attempt')

    # 2. Drop job_diagnostic_events table and indexes
    op.drop_index('idx_diag_events_comp_type', table_name='job_diagnostic_events')
    op.drop_index('idx_diag_events_job_time', table_name='job_diagnostic_events')
    op.drop_table('job_diagnostic_events')
