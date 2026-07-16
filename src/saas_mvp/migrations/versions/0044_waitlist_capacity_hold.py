"""Waitlist capacity hold: slots.held_count + waitlist.hold_party_size.

Revision ID: a4c7e91b6d20
Revises: f8c2d95e7b31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a4c7e91b6d20"
down_revision = "f8c2d95e7b31"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    slot_cols = {c["name"] for c in insp.get_columns("booking_slots")}
    if "held_count" not in slot_cols:
        op.add_column(
            "booking_slots",
            sa.Column("held_count", sa.Integer(), nullable=False, server_default="0"),
        )
    wl_cols = {c["name"] for c in insp.get_columns("booking_waitlist_entries")}
    if "hold_party_size" not in wl_cols:
        op.add_column(
            "booking_waitlist_entries",
            sa.Column("hold_party_size", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("booking_waitlist_entries", "hold_party_size")
    op.drop_column("booking_slots", "held_count")
