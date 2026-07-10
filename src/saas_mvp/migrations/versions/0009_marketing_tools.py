"""行銷工具（A3）：campaign 訊息型別 + 滿意度調查表。

Revision ID: a41c85f7d203
Revises: e6f01b3c8a92
Create Date: 2026-07-10

- marketing_campaigns.message_type / flex_menu_id / image_url：群發支援 Flex/圖片。
- reservation_feedback：預約後 1–5 分滿意度。

冪等守衛：比照 0004–0008。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a41c85f7d203'
down_revision: Union[str, None] = 'e6f01b3c8a92'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    camp_cols = {c["name"] for c in inspector.get_columns("marketing_campaigns")}
    with op.batch_alter_table('marketing_campaigns', schema=None) as batch_op:
        if 'message_type' not in camp_cols:
            batch_op.add_column(sa.Column(
                'message_type', sa.String(length=8),
                server_default='text', nullable=False,
            ))
        if 'flex_menu_id' not in camp_cols:
            batch_op.add_column(sa.Column('flex_menu_id', sa.Integer(), nullable=True))
        if 'image_url' not in camp_cols:
            batch_op.add_column(sa.Column('image_url', sa.String(length=512), nullable=True))

    if 'reservation_feedback' not in inspector.get_table_names():
        op.create_table(
            'reservation_feedback',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column(
                'tenant_id', sa.Integer(),
                sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False,
            ),
            sa.Column(
                'reservation_id', sa.Integer(),
                sa.ForeignKey('booking_reservations.id', ondelete='CASCADE'),
                nullable=False,
            ),
            sa.Column('line_user_id', sa.String(length=64), nullable=False),
            sa.Column('score', sa.Integer(), nullable=True),
            sa.Column('comment', sa.String(length=500), nullable=True),
            sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('responded_at', sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint('reservation_id', name='uq_reservation_feedback'),
        )
        op.create_index('ix_reservation_feedback_id', 'reservation_feedback', ['id'])
        op.create_index(
            'ix_reservation_feedback_tenant_id', 'reservation_feedback', ['tenant_id']
        )


def downgrade() -> None:
    op.drop_table('reservation_feedback')
    with op.batch_alter_table('marketing_campaigns', schema=None) as batch_op:
        batch_op.drop_column('image_url')
        batch_op.drop_column('flex_menu_id')
        batch_op.drop_column('message_type')
