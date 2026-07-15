"""Add bookable rooms/equipment and reservation allocations.

Revision ID: c7e48f19bd32
Revises: b4f92d71ac06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c7e48f19bd32"
down_revision = "b4f92d71ac06"
branch_labels = None
depends_on = None


TABLES = {
    "booking_resource_types",
    "booking_resources",
    "booking_resource_availabilities",
    "booking_resource_blocks",
    "booking_service_resource_requirements",
    "booking_reservation_resource_allocations",
}


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names()) & TABLES
    if existing == TABLES:
        return
    if existing:
        raise RuntimeError(
            "partial bookable resource schema; missing: "
            + ", ".join(sorted(TABLES - existing))
        )

    op.create_table(
        "booking_resource_types",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "tenant_id", "name", name="uq_booking_resource_type_name"
        ),
    )
    op.create_index(
        "ix_booking_resource_types_tenant_id", "booking_resource_types", ["tenant_id"]
    )

    op.create_table(
        "booking_resources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("resource_type_id", sa.Integer(), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("internal_code", sa.String(64), nullable=True),
        sa.Column("capacity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("available_from", sa.Date(), nullable=True),
        sa.Column("available_until", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["resource_type_id"], ["booking_resource_types.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["location_id"], ["booking_locations.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("tenant_id", "name", name="uq_booking_resource_name"),
        sa.UniqueConstraint(
            "tenant_id",
            "internal_code",
            name="uq_booking_resource_internal_code",
        ),
    )
    for column in ("tenant_id", "resource_type_id", "location_id"):
        op.create_index(
            f"ix_booking_resources_{column}", "booking_resources", [column]
        )

    op.create_table(
        "booking_resource_availabilities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["resource_id"], ["booking_resources.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "resource_id",
            "weekday",
            "start_time",
            "end_time",
            name="uq_booking_resource_availability_window",
        ),
    )
    for column in ("tenant_id", "resource_id"):
        op.create_index(
            f"ix_booking_resource_availabilities_{column}",
            "booking_resource_availabilities",
            [column],
        )
    op.create_index(
        "ix_booking_resource_availability_lookup",
        "booking_resource_availabilities",
        ["tenant_id", "resource_id", "weekday"],
    )

    op.create_table(
        "booking_resource_blocks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["resource_id"], ["booking_resources.id"], ondelete="CASCADE"
        ),
    )
    for column in ("tenant_id", "resource_id"):
        op.create_index(
            f"ix_booking_resource_blocks_{column}",
            "booking_resource_blocks",
            [column],
        )
    op.create_index(
        "ix_booking_resource_block_overlap",
        "booking_resource_blocks",
        ["tenant_id", "resource_id", "starts_at", "ends_at"],
    )

    op.create_table(
        "booking_service_resource_requirements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("resource_type_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["service_id"], ["booking_services.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["resource_type_id"], ["booking_resource_types.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "service_id",
            "resource_type_id",
            name="uq_booking_service_resource_requirement",
        ),
    )
    for column in ("tenant_id", "service_id", "resource_type_id"):
        op.create_index(
            f"ix_booking_service_resource_requirements_{column}",
            "booking_service_resource_requirements",
            [column],
        )

    op.create_table(
        "booking_reservation_resource_allocations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("reservation_id", sa.Integer(), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=False),
        sa.Column("resource_type_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resource_name_snapshot", sa.String(128), nullable=False),
        sa.Column("resource_type_name_snapshot", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["reservation_id"], ["booking_reservations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["resource_id"], ["booking_resources.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["resource_type_id"], ["booking_resource_types.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "reservation_id",
            "resource_id",
            name="uq_booking_reservation_resource_allocation",
        ),
    )
    for column in ("tenant_id", "reservation_id", "resource_id", "resource_type_id"):
        op.create_index(
            f"ix_booking_reservation_resource_allocations_{column}",
            "booking_reservation_resource_allocations",
            [column],
        )
    op.create_index(
        "ix_booking_resource_allocation_overlap",
        "booking_reservation_resource_allocations",
        ["tenant_id", "resource_id", "starts_at", "ends_at"],
    )


def downgrade() -> None:
    op.drop_table("booking_reservation_resource_allocations")
    op.drop_table("booking_service_resource_requirements")
    op.drop_table("booking_resource_blocks")
    op.drop_table("booking_resource_availabilities")
    op.drop_table("booking_resources")
    op.drop_table("booking_resource_types")
