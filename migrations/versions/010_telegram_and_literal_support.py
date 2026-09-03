"""Add telegram and literal support
Revision ID: 010_telegram_and_literal_support
Revises: 009_multicore_concurrency
Create Date: 2026-09-02 16:30:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "010_telegram_and_literal_support"
down_revision: Union[str, None] = "009_multicore_concurrency"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Update podcast_jobs columns
    with op.batch_alter_table("podcast_jobs") as batch_op:
        batch_op.add_column(
            sa.Column("transport", sa.String(length=50), nullable=False, server_default="email")
        )
        batch_op.add_column(sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("telegram_message_id", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("telegram_user_id", sa.BigInteger(), nullable=True))
        batch_op.alter_column(
            "gmail_message_id", existing_type=sa.String(length=255), nullable=True
        )
        batch_op.alter_column("sender_email", existing_type=sa.String(length=255), nullable=True)
        batch_op.create_index(
            "uq_podcast_jobs_telegram",
            ["transport", "telegram_chat_id", "telegram_message_id"],
            unique=True,
        )

    # 2. Create telegram_users table
    op.create_table(
        "telegram_users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("first_name", sa.String(length=255), nullable=True),
        sa.Column("role", sa.String(length=50), nullable=False, server_default="owner"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_telegram_users_user_id", "telegram_users", ["telegram_user_id"], unique=True
    )
    op.create_index("idx_telegram_users_chat_id", "telegram_users", ["telegram_chat_id"])

    # 3. Create telegram_pairing_codes table
    op.create_table(
        "telegram_pairing_codes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("is_used", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("used_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_telegram_pairing_code", "telegram_pairing_codes", ["code"], unique=True)
    op.create_index("idx_telegram_pairing_expires", "telegram_pairing_codes", ["expires_at"])

    # 4. Create telegram_poll_state table
    op.create_table(
        "telegram_poll_state",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("last_processed_update_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    # 5. Create telegram_update_failures table
    op.create_table(
        "telegram_update_failures",
        sa.Column("update_id", sa.BigInteger(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("is_dead_lettered", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("update_id"),
    )
    op.create_index(
        "idx_telegram_update_failures_dead_letter", "telegram_update_failures", ["is_dead_lettered"]
    )


def downgrade() -> None:
    # Safe check: detect any rows with NULL gmail_message_id before restoring NOT NULL
    conn = op.get_bind()
    null_rows = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM podcast_jobs WHERE gmail_message_id IS NULL OR sender_email IS NULL"
        )
    ).scalar()
    if null_rows and null_rows > 0:
        raise RuntimeError(
            f"Cannot downgrade database: podcast_jobs table contains {null_rows} row(s) with NULL gmail_message_id "
            "or sender_email. Clean up or migrate these rows before downgrading to a legacy Gmail-only schema."
        )

    op.drop_index("idx_telegram_update_failures_dead_letter", table_name="telegram_update_failures")
    op.drop_table("telegram_update_failures")

    op.drop_table("telegram_poll_state")

    op.drop_index("idx_telegram_pairing_expires", table_name="telegram_pairing_codes")
    op.drop_index("idx_telegram_pairing_code", table_name="telegram_pairing_codes")
    op.drop_table("telegram_pairing_codes")

    op.drop_index("idx_telegram_users_chat_id", table_name="telegram_users")
    op.drop_index("idx_telegram_users_user_id", table_name="telegram_users")
    op.drop_table("telegram_users")

    with op.batch_alter_table("podcast_jobs") as batch_op:
        batch_op.drop_index("uq_podcast_jobs_telegram")
        batch_op.alter_column("sender_email", existing_type=sa.String(length=255), nullable=False)
        batch_op.alter_column(
            "gmail_message_id", existing_type=sa.String(length=255), nullable=False
        )
        batch_op.drop_column("telegram_user_id")
        batch_op.drop_column("telegram_message_id")
        batch_op.drop_column("telegram_chat_id")
        batch_op.drop_column("transport")
