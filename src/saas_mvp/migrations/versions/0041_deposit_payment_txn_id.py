"""Add booking_reservations.deposit_payment_txn_id for LINE Pay deposit binding.

Revision ID: c9f3a61d4b72
Revises: b7e4f19c2a58
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c9f3a61d4b72"
down_revision = "b7e4f19c2a58"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {
        c["name"]
        for c in sa.inspect(op.get_bind()).get_columns("booking_reservations")
    }
    if "deposit_payment_txn_id" not in cols:
        op.add_column(
            "booking_reservations",
            sa.Column("deposit_payment_txn_id", sa.String(length=32), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("booking_reservations", "deposit_payment_txn_id")
