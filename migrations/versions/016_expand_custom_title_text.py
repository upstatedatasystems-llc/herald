"""Expand custom_title to TEXT

Revision ID: 016_expand_custom_title_text
Revises: 015_rerun_lineage_diagnostics
Create Date: 2026-09-09 15:21:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '016_expand_custom_title_text'
down_revision: Union[str, None] = '015_rerun_lineage_diagnostics'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('podcast_jobs') as batch_op:
        batch_op.alter_column('custom_title',
                              existing_type=sa.String(255),
                              type_=sa.Text(),
                              existing_nullable=True)


def downgrade() -> None:
    conn = op.get_bind()
    res = conn.execute(sa.text("SELECT MAX(LENGTH(custom_title)) FROM podcast_jobs WHERE custom_title IS NOT NULL"))
    max_len = res.scalar()
    
    if max_len is not None and max_len > 255:
        count_res = conn.execute(sa.text("SELECT COUNT(*) FROM podcast_jobs WHERE LENGTH(custom_title) > 255"))
        count = count_res.scalar()
        raise Exception(f"Cannot downgrade: {count} custom_title values exceed 255 characters. Truncate or update them before downgrading.")

    with op.batch_alter_table('podcast_jobs') as batch_op:
        batch_op.alter_column('custom_title',
                              existing_type=sa.Text(),
                              type_=sa.String(255),
                              existing_nullable=True)
