"""Reconcile ORM column types with Alembic schema

Revision ID: 004_align_orm_schema
Revises: 003_delivery_idempotency_fields
Create Date: 2026-08-06 14:40:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004_align_orm_schema"
down_revision: Union[str, None] = "003_delivery_idempotency_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Ensure job_state_transitions.from_state is nullable
    with op.batch_alter_table("job_state_transitions") as batch_op:
        batch_op.alter_column("from_state", existing_type=sa.String(length=50), nullable=True)


def downgrade() -> None:
    pass
