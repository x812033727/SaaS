"""LINE 好友狀態 + 歡迎訊息。

Revision ID: 3f8a1c27d6b4
Revises: 0262282689e7
Create Date: 2026-07-10

- booking_customers.line_followed / line_followed_at:webhook follow/unfollow
  事件回寫;行銷推播跳過已封鎖者,不白扣推播額度。
- line_channel_configs.welcome_message:follow 事件的自訂歡迎訊息(NULL=預設文案)。

冪等守衛:比照 0004 — legacy 收斂路徑或手動補欄可能已存在同名欄位,
inspect 後跳過。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '3f8a1c27d6b4'
down_revision: Union[str, None] = '0262282689e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    customer_cols = {c["name"] for c in inspector.get_columns("booking_customers")}
    with op.batch_alter_table('booking_customers', schema=None) as batch_op:
        if 'line_followed' not in customer_cols:
            # 預設 true:歷史顧客無從得知封鎖狀態,沿用「全部視為好友」的原行為。
            batch_op.add_column(sa.Column(
                'line_followed', sa.Boolean(),
                server_default=sa.text('(true)'), nullable=False,
            ))
        if 'line_followed_at' not in customer_cols:
            batch_op.add_column(sa.Column(
                'line_followed_at', sa.DateTime(timezone=True), nullable=True,
            ))

    cfg_cols = {c["name"] for c in inspector.get_columns("line_channel_configs")}
    with op.batch_alter_table('line_channel_configs', schema=None) as batch_op:
        if 'welcome_message' not in cfg_cols:
            batch_op.add_column(sa.Column('welcome_message', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('line_channel_configs', schema=None) as batch_op:
        batch_op.drop_column('welcome_message')
    with op.batch_alter_table('booking_customers', schema=None) as batch_op:
        batch_op.drop_column('line_followed_at')
        batch_op.drop_column('line_followed')
