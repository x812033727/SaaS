"""網頁預約表單 token 表 + line_channel_configs.liff_id（A1.1）。

Revision ID: c2d94ab07e61
Revises: 9b4e72d1a8c5
Create Date: 2026-07-10

- booking_form_tokens：tokenized 網頁預約（比照 pii_requests 模式）。
- line_channel_configs.liff_id：進階選配欄位（自建 LINE Login channel 的租戶
  未來可改以 LIFF 開表單；本版僅留欄位）。

冪等守衛：比照 0004–0006。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c2d94ab07e61'
down_revision: Union[str, None] = '9b4e72d1a8c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    if 'booking_form_tokens' not in inspector.get_table_names():
        op.create_table(
            'booking_form_tokens',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column(
                'tenant_id', sa.Integer(),
                sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False,
            ),
            sa.Column('line_user_id', sa.String(length=64), nullable=False),
            sa.Column('display_name', sa.String(length=128), nullable=True),
            sa.Column('token', sa.String(length=64), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index('ix_booking_form_tokens_id', 'booking_form_tokens', ['id'])
        op.create_index('ix_booking_form_tokens_tenant_id', 'booking_form_tokens', ['tenant_id'])
        op.create_index(
            'ix_booking_form_tokens_token', 'booking_form_tokens', ['token'], unique=True
        )

    cfg_cols = {c["name"] for c in inspector.get_columns("line_channel_configs")}
    with op.batch_alter_table('line_channel_configs', schema=None) as batch_op:
        if 'liff_id' not in cfg_cols:
            batch_op.add_column(sa.Column('liff_id', sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('line_channel_configs', schema=None) as batch_op:
        batch_op.drop_column('liff_id')
    op.drop_table('booking_form_tokens')
