"""定金收款（C4,防 no-show）。

Revision ID: a8d2f61c9e34
Revises: f1c7a3d82e05
Create Date: 2026-07-10

- tenants.deposit_cents / deposit_hold_minutes:店家定金設定(NULL/0=停用)。
- booking_reservations.deposit_*:建單時快照;pending 逾時由 cron 取消回補。

冪等守衛:比照 0004–0014。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a8d2f61c9e34'
down_revision: Union[str, None] = 'f1c7a3d82e05'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    t_cols = {c["name"] for c in inspector.get_columns("tenants")}
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        if 'deposit_cents' not in t_cols:
            batch_op.add_column(sa.Column('deposit_cents', sa.Integer(), nullable=True))
        if 'deposit_hold_minutes' not in t_cols:
            batch_op.add_column(sa.Column('deposit_hold_minutes', sa.Integer(), nullable=True))

    r_cols = {c["name"] for c in inspector.get_columns("booking_reservations")}
    with op.batch_alter_table('booking_reservations', schema=None) as batch_op:
        if 'deposit_cents' not in r_cols:
            batch_op.add_column(sa.Column('deposit_cents', sa.Integer(), nullable=True))
        if 'deposit_status' not in r_cols:
            batch_op.add_column(sa.Column('deposit_status', sa.String(length=16), nullable=True))
        if 'deposit_merchant_trade_no' not in r_cols:
            batch_op.add_column(sa.Column(
                'deposit_merchant_trade_no', sa.String(length=20), nullable=True,
            ))
        if 'deposit_paid_at' not in r_cols:
            batch_op.add_column(sa.Column('deposit_paid_at', sa.DateTime(timezone=True), nullable=True))
        if 'deposit_expires_at' not in r_cols:
            batch_op.add_column(sa.Column('deposit_expires_at', sa.DateTime(timezone=True), nullable=True))
    existing_indexes = {i["name"] for i in inspector.get_indexes("booking_reservations")}
    if 'uq_reservation_deposit_trade_no' not in existing_indexes:
        op.create_index(
            'uq_reservation_deposit_trade_no', 'booking_reservations',
            ['deposit_merchant_trade_no'], unique=True,
        )


def downgrade() -> None:
    op.drop_index('uq_reservation_deposit_trade_no', table_name='booking_reservations')
    with op.batch_alter_table('booking_reservations', schema=None) as batch_op:
        for col in ('deposit_expires_at', 'deposit_paid_at',
                    'deposit_merchant_trade_no', 'deposit_status', 'deposit_cents'):
            batch_op.drop_column(col)
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.drop_column('deposit_hold_minutes')
        batch_op.drop_column('deposit_cents')
