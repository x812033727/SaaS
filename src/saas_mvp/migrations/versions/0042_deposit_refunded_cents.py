"""Add booking_reservations.deposit_refunded_cents for partial deposit refunds.

Revision ID: e2d8b74f6a91
Revises: c9f3a61d4b72
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2d8b74f6a91"
down_revision = "c9f3a61d4b72"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {
        c["name"]
        for c in sa.inspect(op.get_bind()).get_columns("booking_reservations")
    }
    if "deposit_refunded_cents" not in cols:
        op.add_column(
            "booking_reservations",
            sa.Column("deposit_refunded_cents", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("booking_reservations", "deposit_refunded_cents")
