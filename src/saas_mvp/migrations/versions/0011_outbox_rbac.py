"""webhook outbox 強化 + 店內 RBAC（A0.2 + B5）。

Revision ID: c9e3f52a8b17
Revises: b7d2e91f4a56
Create Date: 2026-07-10

- line_webhook_events.payload_json：claim 時落盤原始 event，worker 中途死掉
  可由 ops/retry_stuck_webhook_events 重放（此前 in-flight 任務直接蒸發）。
- users.role：owner / staff（既有使用者回填 owner）。

冪等守衛：比照 0004–0010。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c9e3f52a8b17'
down_revision: Union[str, None] = 'b7d2e91f4a56'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    evt_cols = {c["name"] for c in inspector.get_columns("line_webhook_events")}
    with op.batch_alter_table('line_webhook_events', schema=None) as batch_op:
        if 'payload_json' not in evt_cols:
            batch_op.add_column(sa.Column('payload_json', sa.Text(), nullable=True))

    user_cols = {c["name"] for c in inspector.get_columns("users")}
    with op.batch_alter_table('users', schema=None) as batch_op:
        if 'role' not in user_cols:
            # server_default='owner'：既有使用者全部回填 owner（每租戶目前一人）。
            batch_op.add_column(sa.Column(
                'role', sa.String(length=16),
                server_default='owner', nullable=False,
            ))


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('role')
    with op.batch_alter_table('line_webhook_events', schema=None) as batch_op:
        batch_op.drop_column('payload_json')
