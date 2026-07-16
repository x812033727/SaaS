"""Add orders.payment_txn_id for LINE Pay txid↔order binding.

Revision ID: b7e4f19c2a58
Revises: d39a2c83b5d7
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b7e4f19c2a58"
down_revision = "d39a2c83b5d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("orders")}
    if "payment_txn_id" not in cols:
        op.add_column(
            "orders", sa.Column("payment_txn_id", sa.String(length=32), nullable=True)
        )
        op.create_index(
            "ix_orders_payment_txn_id", "orders", ["payment_txn_id"], unique=False
        )


def downgrade() -> None:
    op.drop_index("ix_orders_payment_txn_id", table_name="orders")
    op.drop_column("orders", "payment_txn_id")
