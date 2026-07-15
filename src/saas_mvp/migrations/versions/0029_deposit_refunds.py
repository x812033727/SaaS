"""Add deposit payment snapshots and refund workflow fields.

Revision ID: a3c7e19f5b42
Revises: 9b1f6a2e4c80
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a3c7e19f5b42"
down_revision = "9b1f6a2e4c80"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("booking_reservations")
    }
    columns = (
        sa.Column("deposit_provider", sa.String(16), nullable=True),
        sa.Column("deposit_provider_merchant_id", sa.String(64), nullable=True),
        sa.Column("deposit_provider_trade_no", sa.String(20), nullable=True),
        sa.Column("deposit_payment_type", sa.String(32), nullable=True),
        sa.Column("deposit_refund_status", sa.String(24), nullable=True),
        sa.Column(
            "deposit_refund_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("deposit_refund_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deposit_refunded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deposit_refund_error", sa.String(255), nullable=True),
        sa.Column("deposit_refund_provider_code", sa.String(32), nullable=True),
        sa.Column("deposit_refund_requested_by_user_id", sa.Integer(), nullable=True),
    )
    with op.batch_alter_table("booking_reservations") as batch_op:
        for column in columns:
            if column.name not in existing:
                batch_op.add_column(column)


def downgrade() -> None:
    existing = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("booking_reservations")
    }
    with op.batch_alter_table("booking_reservations") as batch_op:
        for name in (
            "deposit_refund_requested_by_user_id",
            "deposit_refund_provider_code",
            "deposit_refund_error",
            "deposit_refunded_at",
            "deposit_refund_requested_at",
            "deposit_refund_attempts",
            "deposit_refund_status",
            "deposit_payment_type",
            "deposit_provider_trade_no",
            "deposit_provider_merchant_id",
            "deposit_provider",
        ):
            if name in existing:
                batch_op.drop_column(name)
