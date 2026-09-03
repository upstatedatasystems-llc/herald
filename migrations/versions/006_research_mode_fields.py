"""Add research mode fields and script/research Drive artifact columns

Revision ID: 006_research_mode_fields
Revises: 005_drive_artifacts
Create Date: 2026-08-07 22:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = '006_research_mode_fields'
down_revision: Union[str, None] = '005_drive_artifacts'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('podcast_jobs') as batch_op:
        batch_op.add_column(sa.Column('research_depth', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('research_grounding_json', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('research_json', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('research_model', sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column('research_search_count', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('research_source_count', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('research_audit_json', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('research_repair_count', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('script_drive_file_id', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('script_drive_web_link', sa.String(length=512), nullable=True))
        batch_op.add_column(sa.Column('research_drive_file_id', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('research_drive_web_link', sa.String(length=512), nullable=True))
        batch_op.add_column(sa.Column('research_notes_drive_file_id', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('research_notes_drive_web_link', sa.String(length=512), nullable=True))

        batch_op.create_index('idx_podcast_jobs_script_drive_file_id', ['script_drive_file_id'], unique=True)
        batch_op.create_index('idx_podcast_jobs_research_drive_file_id', ['research_drive_file_id'], unique=True)
        batch_op.create_index('idx_podcast_jobs_research_notes_drive_file_id', ['research_notes_drive_file_id'], unique=True)


def downgrade() -> None:
    with op.batch_alter_table('podcast_jobs') as batch_op:
        batch_op.drop_index('idx_podcast_jobs_research_notes_drive_file_id')
        batch_op.drop_index('idx_podcast_jobs_research_drive_file_id')
        batch_op.drop_index('idx_podcast_jobs_script_drive_file_id')
        batch_op.drop_column('research_notes_drive_web_link')
        batch_op.drop_column('research_notes_drive_file_id')
        batch_op.drop_column('research_drive_web_link')
        batch_op.drop_column('research_drive_file_id')
        batch_op.drop_column('script_drive_web_link')
        batch_op.drop_column('script_drive_file_id')
        batch_op.drop_column('research_repair_count')
        batch_op.drop_column('research_audit_json')
        batch_op.drop_column('research_source_count')
        batch_op.drop_column('research_search_count')
        batch_op.drop_column('research_model')
        batch_op.drop_column('research_json')
        batch_op.drop_column('research_grounding_json')
        batch_op.drop_column('research_depth')
