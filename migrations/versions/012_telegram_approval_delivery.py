"""Add telegram approval and delivery metadata

Revision ID: 012_telegram_approval_delivery
Revises: 011_telegram_user_preferences
Create Date: 2026-09-03 14:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "012_telegram_approval_delivery"
down_revision: Union[str, None] = "011_telegram_user_preferences"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("podcast_jobs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "approval_required", sa.Boolean(), nullable=False, server_default=sa.text("false")
            )
        )
        batch_op.add_column(
            sa.Column("approval_requested_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column("telegram_approval_message_id", sa.BigInteger(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("telegram_delivery_message_id", sa.BigInteger(), nullable=True)
        )
        batch_op.add_column(sa.Column("telegram_audio_file_id", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("telegram_document_file_id", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("podcast_jobs") as batch_op:
        batch_op.drop_column("telegram_document_file_id")
        batch_op.drop_column("telegram_audio_file_id")
        batch_op.drop_column("telegram_delivery_message_id")
        batch_op.drop_column("telegram_approval_message_id")
        batch_op.drop_column("approved_at")
        batch_op.drop_column("approval_requested_at")
        batch_op.drop_column("approval_required")
