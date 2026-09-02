"""Add telegram and literal support
Revision ID: 010_telegram_and_literal_support
Revises: 009_multicore_concurrency
Create Date: 2026-09-02 16:30:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = '010_telegram_and_literal_support'
down_revision: Union[str, None] = '009_multicore_concurrency'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Update podcast_jobs columns
    with op.batch_alter_table('podcast_jobs') as batch_op:
        batch_op.add_column(sa.Column('transport', sa.String(length=50), nullable=False, server_default='email'))
        batch_op.add_column(sa.Column('telegram_chat_id', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('telegram_message_id', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('telegram_user_id', sa.String(length=100), nullable=True))
        batch_op.alter_column('gmail_message_id', existing_type=sa.String(length=255), nullable=True)
        batch_op.alter_column('sender_email', existing_type=sa.String(length=255), nullable=True)
        batch_op.create_index('idx_podcast_jobs_telegram', ['transport', 'telegram_chat_id', 'telegram_message_id'])

    # 2. Create telegram_users table
    op.create_table(
        'telegram_users',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('telegram_user_id', sa.BigInteger(), nullable=False),
        sa.Column('telegram_chat_id', sa.String(length=100), nullable=False),
        sa.Column('username', sa.String(length=255), nullable=True),
        sa.Column('first_name', sa.String(length=255), nullable=True),
        sa.Column('role', sa.String(length=50), nullable=False, server_default='owner'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_telegram_users_user_id', 'telegram_users', ['telegram_user_id'], unique=True)
    op.create_index('idx_telegram_users_chat_id', 'telegram_users', ['telegram_chat_id'])

    # 3. Create telegram_pairing_codes table
    op.create_table(
        'telegram_pairing_codes',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('code', sa.String(length=50), nullable=False),
        sa.Column('is_used', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('used_by_user_id', sa.BigInteger(), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_telegram_pairing_code', 'telegram_pairing_codes', ['code'], unique=True)
    op.create_index('idx_telegram_pairing_expires', 'telegram_pairing_codes', ['expires_at'])


def downgrade() -> None:
    op.drop_index('idx_telegram_pairing_expires', table_name='telegram_pairing_codes')
    op.drop_index('idx_telegram_pairing_code', table_name='telegram_pairing_codes')
    op.drop_table('telegram_pairing_codes')

    op.drop_index('idx_telegram_users_chat_id', table_name='telegram_users')
    op.drop_index('idx_telegram_users_user_id', table_name='telegram_users')
    op.drop_table('telegram_users')

    with op.batch_alter_table('podcast_jobs') as batch_op:
        batch_op.drop_index('idx_podcast_jobs_telegram')
        batch_op.drop_column('telegram_user_id')
        batch_op.drop_column('telegram_message_id')
        batch_op.drop_column('telegram_chat_id')
        batch_op.drop_column('transport')
