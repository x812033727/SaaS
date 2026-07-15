"""Add expiring and fulfillable waitlist offers.

Revision ID: c4e8f21a6d73
Revises: a3c7e19f5b42
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c4e8f21a6d73"
down_revision = "a3c7e19f5b42"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table)
    }


def upgrade() -> None:
    tenant_columns = _columns("tenants")
    if "waitlist_offer_minutes" not in tenant_columns:
        with op.batch_alter_table("tenants") as batch_op:
            batch_op.add_column(
                sa.Column("waitlist_offer_minutes", sa.Integer(), nullable=True)
            )

    existing = _columns("booking_waitlist_entries")
    columns = (
        sa.Column("offer_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "notification_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("service_id", sa.Integer(), nullable=True),
        sa.Column("staff_id", sa.Integer(), nullable=True),
        sa.Column("reservation_id", sa.Integer(), nullable=True),
    )
    with op.batch_alter_table("booking_waitlist_entries") as batch_op:
        for column in columns:
            if column.name not in existing:
                batch_op.add_column(column)

    foreign_keys = sa.inspect(op.get_bind()).get_foreign_keys(
        "booking_waitlist_entries"
    )
    has_reservation_fk = any(
        fk.get("constrained_columns") == ["reservation_id"]
        and fk.get("referred_table") == "booking_reservations"
        for fk in foreign_keys
    )
    if not has_reservation_fk:
        with op.batch_alter_table("booking_waitlist_entries") as batch_op:
            batch_op.create_foreign_key(
                "fk_booking_waitlist_reservation_id",
                "booking_reservations",
                ["reservation_id"],
                ["id"],
                ondelete="SET NULL",
            )

    indexes = {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes("booking_waitlist_entries")
    }
    if "ix_booking_waitlist_entries_reservation_id" not in indexes:
        op.create_index(
            "ix_booking_waitlist_entries_reservation_id",
            "booking_waitlist_entries",
            ["reservation_id"],
            unique=False,
        )


def downgrade() -> None:
    indexes = {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes("booking_waitlist_entries")
    }
    if "ix_booking_waitlist_entries_reservation_id" in indexes:
        op.drop_index(
            "ix_booking_waitlist_entries_reservation_id",
            table_name="booking_waitlist_entries",
        )
    foreign_keys = sa.inspect(op.get_bind()).get_foreign_keys(
        "booking_waitlist_entries"
    )
    reservation_fk = next(
        (
            fk
            for fk in foreign_keys
            if fk.get("constrained_columns") == ["reservation_id"]
            and fk.get("referred_table") == "booking_reservations"
        ),
        None,
    )
    if reservation_fk is not None and reservation_fk.get("name"):
        with op.batch_alter_table("booking_waitlist_entries") as batch_op:
            batch_op.drop_constraint(
                reservation_fk["name"], type_="foreignkey"
            )
    existing = _columns("booking_waitlist_entries")
    with op.batch_alter_table("booking_waitlist_entries") as batch_op:
        for name in (
            "reservation_id",
            "staff_id",
            "service_id",
            "notification_attempts",
            "offer_expires_at",
        ):
            if name in existing:
                batch_op.drop_column(name)
    if "waitlist_offer_minutes" in _columns("tenants"):
        with op.batch_alter_table("tenants") as batch_op:
            batch_op.drop_column("waitlist_offer_minutes")
