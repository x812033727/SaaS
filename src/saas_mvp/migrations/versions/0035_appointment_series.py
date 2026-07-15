"""Add recurring appointment series and occurrence outcomes.

Revision ID: e83c4b1a7d29
Revises: c7e48f19bd32
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e83c4b1a7d29"
down_revision = "c7e48f19bd32"
branch_labels = None
depends_on = None


TABLES = {
    "booking_appointment_series",
    "booking_appointment_series_occurrences",
}


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names()) & TABLES
    if existing == TABLES:
        return
    if existing:
        raise RuntimeError(
            "partial appointment series schema; missing: "
            + ", ".join(sorted(TABLES - existing))
        )

    op.create_table(
        "booking_appointment_series",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("source_reservation_id", sa.Integer(), nullable=True),
        sa.Column("recurrence_unit", sa.String(16), nullable=False),
        sa.Column(
            "recurrence_interval", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column("requested_occurrences", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_reservation_id"],
            ["booking_reservations.id"],
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_booking_appointment_series_tenant_id",
        "booking_appointment_series",
        ["tenant_id"],
    )
    op.create_index(
        "ix_booking_appointment_series_source_reservation_id",
        "booking_appointment_series",
        ["source_reservation_id"],
    )
    op.create_index(
        "ix_booking_appointment_series_tenant_status",
        "booking_appointment_series",
        ["tenant_id", "status"],
    )

    op.create_table(
        "booking_appointment_series_occurrences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("series_id", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("target_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reservation_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("conflict_reason", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["series_id"], ["booking_appointment_series.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"], ["booking_reservations.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("reservation_id"),
        sa.UniqueConstraint(
            "series_id", "sequence", name="uq_booking_appointment_series_sequence"
        ),
    )
    for column in ("tenant_id", "series_id", "target_start"):
        op.create_index(
            f"ix_booking_appointment_series_occurrences_{column}",
            "booking_appointment_series_occurrences",
            [column],
        )
    op.create_index(
        "ix_booking_appointment_occurrence_tenant_status",
        "booking_appointment_series_occurrences",
        ["tenant_id", "status"],
    )


def downgrade() -> None:
    op.drop_table("booking_appointment_series_occurrences")
    op.drop_table("booking_appointment_series")
