"""Add commercial service packages and credit ledger.

Revision ID: f6b1d4208a31
Revises: c4e8f21a6d73
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f6b1d4208a31"
down_revision = "c4e8f21a6d73"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # legacy_init_db 會以「目前 model metadata」先收斂未納管的舊資料庫，
    # 因而四張新表可能在 stamp baseline 前就已完整存在。完整存在時本 revision
    # 應為 no-op；只存在部分則代表曾有非交易式 DDL 中斷，明確停止讓維運介入，
    # 不以 create_table already exists 掩蓋不完整 schema。
    expected = {
        "service_packages",
        "service_package_items",
        "customer_packages",
        "package_credit_ledger",
    }
    existing = set(sa.inspect(op.get_bind()).get_table_names()) & expected
    if existing == expected:
        return
    if existing:
        missing = ", ".join(sorted(expected - existing))
        raise RuntimeError(f"partial service package schema; missing: {missing}")

    op.create_table(
        "service_packages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("price_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("validity_days", sa.Integer(), nullable=False, server_default="365"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_service_package_tenant_name"),
    )
    op.create_index("ix_service_packages_tenant_id", "service_packages", ["tenant_id"])

    op.create_table(
        "service_package_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("package_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("included_quantity", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["package_id"], ["service_packages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["service_id"], ["booking_services.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("package_id", "service_id", name="uq_service_package_item"),
    )
    op.create_index("ix_service_package_items_tenant_id", "service_package_items", ["tenant_id"])
    op.create_index("ix_service_package_items_package_id", "service_package_items", ["package_id"])
    op.create_index("ix_service_package_items_service_id", "service_package_items", ["service_id"])

    op.create_table(
        "customer_packages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("package_id", sa.Integer(), nullable=False),
        sa.Column("package_name_snapshot", sa.String(128), nullable=False),
        sa.Column("price_cents_snapshot", sa.Integer(), nullable=False),
        sa.Column("issuance_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("issued_by_user_id", sa.Integer(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["booking_customers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["package_id"], ["service_packages.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("tenant_id", "issuance_key", name="uq_customer_package_issuance_key"),
    )
    op.create_index("ix_customer_packages_tenant_id", "customer_packages", ["tenant_id"])
    op.create_index("ix_customer_packages_customer_id", "customer_packages", ["customer_id"])
    op.create_index("ix_customer_packages_package_id", "customer_packages", ["package_id"])
    op.create_index("ix_customer_packages_expires_at", "customer_packages", ["expires_at"])
    op.create_index(
        "ix_customer_package_tenant_customer_status",
        "customer_packages",
        ["tenant_id", "customer_id", "status"],
    )

    op.create_table(
        "package_credit_ledger",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("customer_package_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("reservation_id", sa.Integer(), nullable=True),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("note", sa.String(255), nullable=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_package_id"], ["customer_packages.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["customer_id"], ["booking_customers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["service_id"], ["booking_services.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reservation_id"], ["booking_reservations.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("tenant_id", "reservation_id", "kind", name="uq_package_ledger_reservation_kind"),
    )
    for column in ("tenant_id", "customer_package_id", "customer_id", "service_id", "reservation_id"):
        op.create_index(f"ix_package_credit_ledger_{column}", "package_credit_ledger", [column])
    op.create_index(
        "ix_package_ledger_balance",
        "package_credit_ledger",
        ["tenant_id", "customer_package_id", "service_id"],
    )


def downgrade() -> None:
    op.drop_table("package_credit_ledger")
    op.drop_table("customer_packages")
    op.drop_table("service_package_items")
    op.drop_table("service_packages")
