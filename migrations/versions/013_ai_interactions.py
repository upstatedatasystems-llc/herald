"""Add ai_interactions table for persistent AI evidence and diagnostics

Revision ID: 013_ai_interactions
Revises: 012_telegram_approval_delivery
Create Date: 2026-09-03 17:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '013_ai_interactions'
down_revision: Union[str, None] = '012_telegram_approval_delivery'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'ai_interactions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('job_id', sa.String(length=36), nullable=True),
        sa.Column('provider', sa.String(length=50), nullable=False),
        sa.Column('model', sa.String(length=100), nullable=False),
        sa.Column('operation', sa.String(length=50), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('duration_ms', sa.BigInteger(), nullable=True),
        sa.Column('success', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('prompt_tokens', sa.Integer(), nullable=True),
        sa.Column('completion_tokens', sa.Integer(), nullable=True),
        sa.Column('total_tokens', sa.Integer(), nullable=True),
        sa.Column('error_category', sa.String(length=100), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.ForeignKeyConstraint(['job_id'], ['podcast_jobs.id'], name='fk_ai_interactions_job_id', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name='pk_ai_interactions'),
    )
    op.create_index('idx_ai_interactions_job_created', 'ai_interactions', ['job_id', 'created_at'], unique=False)
    op.create_index('idx_ai_interactions_provider_created', 'ai_interactions', ['provider', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('idx_ai_interactions_provider_created', table_name='ai_interactions')
    op.drop_index('idx_ai_interactions_job_created', table_name='ai_interactions')
    op.drop_table('ai_interactions')
