"""Delivery idempotency fields migration

Revision ID: 003_delivery_idempotency_fields
Revises: 002_hardened_schema
Create Date: 2026-08-06 14:20:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003_delivery_idempotency_fields"
down_revision: Union[str, None] = "002_hardened_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("podcast_jobs", sa.Column("drive_job_key", sa.String(length=100), nullable=True))
    op.add_column(
        "podcast_jobs", sa.Column("gmail_result_message_id", sa.String(length=255), nullable=True)
    )
    op.create_index("idx_podcast_jobs_drive_job_key", "podcast_jobs", ["drive_job_key"])


def downgrade() -> None:
    op.drop_index("idx_podcast_jobs_drive_job_key", table_name="podcast_jobs")
    op.drop_column("podcast_jobs", "gmail_result_message_id")
    op.drop_column("podcast_jobs", "drive_job_key")
