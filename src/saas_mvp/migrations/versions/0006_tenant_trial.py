"""tenants 試用欄位（B1 變現翻正）。

Revision ID: 9b4e72d1a8c5
Revises: 3f8a1c27d6b4
Create Date: 2026-07-10

- tenants.trial_plan / trial_ends_at：試用（與既有租戶 grandfathering 共用機制），
  effective_plan（services/plans.py）純計算、到期即刻生效。

冪等守衛：比照 0004/0005。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '9b4e72d1a8c5'
down_revision: Union[str, None] = '3f8a1c27d6b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("tenants")}
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        if 'trial_plan' not in existing:
            batch_op.add_column(sa.Column('trial_plan', sa.String(length=32), nullable=True))
        if 'trial_ends_at' not in existing:
            batch_op.add_column(sa.Column('trial_ends_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.drop_column('trial_ends_at')
        batch_op.drop_column('trial_plan')
