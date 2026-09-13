"""Interactive podcast parameters, long-form research, and deterministic intro/outro

Revision ID: 018_interactive_params_longform
Revises: 017_vendor_neutral_failover
Create Date: 2026-09-13 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "018_interactive_params_longform"
down_revision: Union[str, None] = "017_vendor_neutral_failover"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add new columns to podcast_jobs
    with op.batch_alter_table("podcast_jobs") as batch_op:
        batch_op.add_column(sa.Column("content_mode", sa.String(20), nullable=True))
        batch_op.add_column(sa.Column("target_minutes", sa.String(20), nullable=True))
        batch_op.add_column(sa.Column("research_plan_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("evidence_packet_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("outline_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("section_progress_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("fidelity_audit_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("branding_intro_seconds", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("branding_outro_seconds", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("program_duration_seconds", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("configuration_state_json", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column("resolved_default", sa.Boolean(), nullable=True, server_default=sa.false())
        )
        batch_op.add_column(sa.Column("telegram_config_message_id", sa.BigInteger(), nullable=True))

    # 2. Add new user defaults columns to telegram_users
    with op.batch_alter_table("telegram_users") as batch_op:
        batch_op.add_column(
            sa.Column("default_content_mode", sa.String(20), nullable=True, server_default="source")
        )
        batch_op.add_column(
            sa.Column("default_target_minutes", sa.String(20), nullable=True, server_default="auto")
        )
        batch_op.add_column(
            sa.Column("default_research_depth", sa.String(20), nullable=True, server_default="medium")
        )

    # 3. Deterministic Historical Backfill for existing podcast_jobs
    conn = op.get_bind()
    jobs = conn.execute(
        sa.text("SELECT id, request_mode, research_depth FROM podcast_jobs WHERE content_mode IS NULL")
    ).fetchall()

    for job in jobs:
        job_id = job[0]
        req_mode = (job[1] or "standard").lower().strip()
        r_depth = job[2]

        if req_mode == "literal":
            c_mode = "literal"
            t_mins = "auto"
            res_depth = None
        elif req_mode in ("research", "detailed"):
            c_mode = "expanded"
            t_mins = "auto"
            res_depth = r_depth or "medium"
        else:
            c_mode = "source"
            t_mins = "auto"
            res_depth = None

        conn.execute(
            sa.text("""
                UPDATE podcast_jobs
                SET content_mode = :c_mode,
                    target_minutes = :t_mins,
                    research_depth = COALESCE(research_depth, :res_depth)
                WHERE id = :jid
            """),
            {
                "c_mode": c_mode,
                "t_mins": t_mins,
                "res_depth": res_depth,
                "jid": job_id,
            },
        )


def downgrade() -> None:
    with op.batch_alter_table("telegram_users") as batch_op:
        batch_op.drop_column("default_research_depth")
        batch_op.drop_column("default_target_minutes")
        batch_op.drop_column("default_content_mode")

    with op.batch_alter_table("podcast_jobs") as batch_op:
        batch_op.drop_column("telegram_config_message_id")
        batch_op.drop_column("resolved_default")
        batch_op.drop_column("configuration_state_json")
        batch_op.drop_column("program_duration_seconds")
        batch_op.drop_column("branding_outro_seconds")
        batch_op.drop_column("branding_intro_seconds")
        batch_op.drop_column("fidelity_audit_json")
        batch_op.drop_column("section_progress_json")
        batch_op.drop_column("outline_json")
        batch_op.drop_column("evidence_packet_json")
        batch_op.drop_column("research_plan_json")
        batch_op.drop_column("target_minutes")
        batch_op.drop_column("content_mode")
