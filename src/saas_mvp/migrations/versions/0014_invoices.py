"""電子發票表（C2）。

Revision ID: f1c7a3d82e05
Revises: e2b6d90c3a41
Create Date: 2026-07-10

冪等守衛:比照 0004–0013。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f1c7a3d82e05'
down_revision: Union[str, None] = 'e2b6d90c3a41'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if 'invoices' in inspector.get_table_names():
        return
    op.create_table(
        'invoices',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column(
            'tenant_id', sa.Integer(),
            sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False,
        ),
        sa.Column(
            'subscription_charge_id', sa.Integer(),
            sa.ForeignKey('subscription_charges.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column('order_id', sa.Integer(), nullable=True),
        sa.Column('relate_number', sa.String(length=30), nullable=False),
        sa.Column('invoice_no', sa.String(length=16), nullable=True),
        sa.Column('invoice_date', sa.String(length=32), nullable=True),
        sa.Column('random_number', sa.String(length=8), nullable=True),
        sa.Column('amount_cents', sa.Integer(), nullable=False),
        sa.Column('buyer_email', sa.String(length=256), nullable=True),
        sa.Column('status', sa.String(length=8), nullable=False),
        sa.Column('provider', sa.String(length=8), nullable=False),
        sa.Column('error_msg', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('issued_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('subscription_charge_id', name='uq_invoice_charge'),
        sa.UniqueConstraint('relate_number', name='uq_invoice_relate'),
    )
    op.create_index('ix_invoices_id', 'invoices', ['id'])
    op.create_index('ix_invoices_tenant_id', 'invoices', ['tenant_id'])


def downgrade() -> None:
    op.drop_table('invoices')
