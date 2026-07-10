"""webhook 事件診斷欄位（F3,M2-003）。

Revision ID: e2b6d90c3a41
Revises: d5a8c14e7f92
Create Date: 2026-07-10

- line_webhook_events.event_type:claim 時抽 event.type,供健康報表分組。
- line_webhook_events.error_detail:失敗時存遮罩後 traceback 摘要
  （last_error 維持 exception class 的安全預設）。

冪等守衛:比照 0004–0012。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e2b6d90c3a41'
down_revision: Union[str, None] = 'd5a8c14e7f92'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    cols = {c["name"] for c in inspector.get_columns("line_webhook_events")}
    with op.batch_alter_table('line_webhook_events', schema=None) as batch_op:
        if 'event_type' not in cols:
            batch_op.add_column(sa.Column('event_type', sa.String(length=32), nullable=True))
        if 'error_detail' not in cols:
            batch_op.add_column(sa.Column('error_detail', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('line_webhook_events', schema=None) as batch_op:
        batch_op.drop_column('error_detail')
        batch_op.drop_column('event_type')
