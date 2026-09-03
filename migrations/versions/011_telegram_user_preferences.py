"""Add telegram user preferences

Revision ID: 011_telegram_user_preferences
Revises: 010_telegram_and_literal_support
Create Date: 2026-09-03 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "011_telegram_user_preferences"
down_revision: Union[str, None] = "010_telegram_and_literal_support"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("telegram_users") as batch_op:
        batch_op.add_column(
            sa.Column(
                "confirm_before_tts", sa.Boolean(), nullable=False, server_default=sa.text("false")
            )
        )
        batch_op.add_column(sa.Column("default_voice", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("default_speed", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("default_mode", sa.String(length=20), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("telegram_users") as batch_op:
        batch_op.drop_column("default_mode")
        batch_op.drop_column("default_speed")
        batch_op.drop_column("default_voice")
        batch_op.drop_column("confirm_before_tts")
