"""Add POS staff attribution, commission earnings and pay runs.

Revision ID: a91d7c4e2b60
Revises: e83c4b1a7d29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a91d7c4e2b60"
down_revision = "e83c4b1a7d29"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    order_columns = {column["name"] for column in inspector.get_columns("orders")}
    order_item_columns = {
        column["name"] for column in inspector.get_columns("order_items")
    }
    # batch mode 在 PostgreSQL 仍走一般 ALTER，在 SQLite 自動 copy-and-move，
    # 使開發環境也能為既有資料表新增 FK／unique constraint。
    # legacy_init_db() 會先以當前 metadata 補齊 schema；此時必須跳過
    # 已存在的欄位與表，否則 SQLite batch 重建會重複排序與建約束。
    if not {
        "points_cents",
        "reservation_id",
        "staff_id",
        "payment_method",
        "tip_cents",
    }.issubset(order_columns):
        with op.batch_alter_table("orders") as batch:
            batch.add_column(
                sa.Column(
                    "points_cents", sa.Integer(), nullable=False, server_default="0"
                )
            )
            batch.add_column(sa.Column("reservation_id", sa.Integer(), nullable=True))
            batch.add_column(sa.Column("staff_id", sa.Integer(), nullable=True))
            batch.add_column(sa.Column("payment_method", sa.String(16), nullable=True))
            batch.add_column(
                sa.Column("tip_cents", sa.Integer(), nullable=False, server_default="0")
            )
            batch.create_foreign_key(
                "fk_orders_reservation_id",
                "booking_reservations",
                ["reservation_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch.create_foreign_key(
                "fk_orders_staff_id",
                "booking_staff",
                ["staff_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch.create_unique_constraint(
                "uq_orders_reservation_id", ["reservation_id"]
            )
            batch.create_index("ix_orders_staff_id", ["staff_id"])
            batch.create_index(
                "ix_order_tenant_staff_paid", ["tenant_id", "staff_id", "paid_at"]
            )

    if not {"service_id", "staff_id", "item_type"}.issubset(order_item_columns):
        with op.batch_alter_table("order_items") as batch:
            batch.add_column(sa.Column("service_id", sa.Integer(), nullable=True))
            batch.add_column(sa.Column("staff_id", sa.Integer(), nullable=True))
            batch.add_column(
                sa.Column(
                    "item_type", sa.String(16), nullable=False, server_default="product"
                )
            )
            batch.create_foreign_key(
                "fk_order_items_service_id",
                "booking_services",
                ["service_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch.create_foreign_key(
                "fk_order_items_staff_id",
                "booking_staff",
                ["staff_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch.create_index("ix_order_items_staff_id", ["staff_id"])

    if "staff_commission_rules" not in tables:
        op.create_table(
            "staff_commission_rules",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), nullable=False),
            sa.Column("staff_id", sa.Integer(), nullable=False),
            sa.Column("item_type", sa.String(16), nullable=False),
            sa.Column("method", sa.String(16), nullable=False),
            sa.Column("value", sa.Integer(), nullable=False),
            sa.Column(
                "calculation_basis", sa.String(16), nullable=False, server_default="net"
            ),
            sa.Column("effective_from", sa.Date(), nullable=False),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("created_by_user_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["staff_id"], ["booking_staff.id"], ondelete="CASCADE"
            ),
        )
        op.create_index(
            "ix_staff_commission_rules_tenant_id",
            "staff_commission_rules",
            ["tenant_id"],
        )
        op.create_index(
            "ix_staff_commission_rules_staff_id", "staff_commission_rules", ["staff_id"]
        )
        op.create_index(
            "ix_staff_commission_rule_lookup",
            "staff_commission_rules",
            ["tenant_id", "staff_id", "item_type", "effective_from"],
        )

    if "staff_pay_runs" not in tables:
        op.create_table(
            "staff_pay_runs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), nullable=False),
            sa.Column("period_start", sa.Date(), nullable=False),
            sa.Column("period_end", sa.Date(), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
            sa.Column("total_cents", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_by_user_id", sa.Integer(), nullable=True),
            sa.Column("finalized_by_user_id", sa.Integer(), nullable=True),
            sa.Column("paid_by_user_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_staff_pay_runs_tenant_id", "staff_pay_runs", ["tenant_id"])
        op.create_index(
            "ix_staff_pay_run_tenant_period",
            "staff_pay_runs",
            ["tenant_id", "period_start", "period_end"],
        )

    if "staff_commission_earnings" not in tables:
        op.create_table(
            "staff_commission_earnings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), nullable=False),
            sa.Column("staff_id", sa.Integer(), nullable=False),
            sa.Column("order_id", sa.Integer(), nullable=False),
            sa.Column("order_item_id", sa.Integer(), nullable=True),
            sa.Column("pay_run_id", sa.Integer(), nullable=True),
            sa.Column("reversal_of_id", sa.Integer(), nullable=True),
            sa.Column("source_key", sa.String(64), nullable=False),
            sa.Column("item_type", sa.String(16), nullable=False),
            sa.Column("item_name_snapshot", sa.String(128), nullable=False),
            sa.Column("gross_cents", sa.Integer(), nullable=False),
            sa.Column("net_cents", sa.Integer(), nullable=False),
            sa.Column("calculation_basis", sa.String(16), nullable=False),
            sa.Column("method_snapshot", sa.String(16), nullable=False),
            sa.Column("value_snapshot", sa.Integer(), nullable=False),
            sa.Column("commission_cents", sa.Integer(), nullable=False),
            sa.Column("earned_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["staff_id"], ["booking_staff.id"], ondelete="RESTRICT"
            ),
            sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(
                ["order_item_id"], ["order_items.id"], ondelete="RESTRICT"
            ),
            sa.ForeignKeyConstraint(
                ["pay_run_id"], ["staff_pay_runs.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["reversal_of_id"],
                ["staff_commission_earnings.id"],
                ondelete="RESTRICT",
            ),
            sa.UniqueConstraint(
                "tenant_id", "source_key", name="uq_commission_earning_source"
            ),
            sa.UniqueConstraint("reversal_of_id"),
        )
        for column in ("tenant_id", "staff_id", "order_id", "pay_run_id"):
            op.create_index(
                f"ix_staff_commission_earnings_{column}",
                "staff_commission_earnings",
                [column],
            )
        op.create_index(
            "ix_commission_earning_unsettled",
            "staff_commission_earnings",
            ["tenant_id", "pay_run_id", "earned_at"],
        )

    if "staff_pay_run_items" not in tables:
        op.create_table(
            "staff_pay_run_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), nullable=False),
            sa.Column("pay_run_id", sa.Integer(), nullable=False),
            sa.Column("staff_id", sa.Integer(), nullable=False),
            sa.Column(
                "commission_cents", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("tip_cents", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "adjustment_cents", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("total_cents", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("adjustment_note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["pay_run_id"], ["staff_pay_runs.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["staff_id"], ["booking_staff.id"], ondelete="RESTRICT"
            ),
            sa.UniqueConstraint("pay_run_id", "staff_id", name="uq_staff_pay_run_item"),
        )
        for column in ("tenant_id", "pay_run_id", "staff_id"):
            op.create_index(
                f"ix_staff_pay_run_items_{column}", "staff_pay_run_items", [column]
            )


def downgrade() -> None:
    op.drop_table("staff_pay_run_items")
    op.drop_table("staff_commission_earnings")
    op.drop_table("staff_pay_runs")
    op.drop_table("staff_commission_rules")
    with op.batch_alter_table("order_items") as batch:
        batch.drop_index("ix_order_items_staff_id")
        batch.drop_constraint("fk_order_items_staff_id", type_="foreignkey")
        batch.drop_constraint("fk_order_items_service_id", type_="foreignkey")
        batch.drop_column("item_type")
        batch.drop_column("staff_id")
        batch.drop_column("service_id")
    with op.batch_alter_table("orders") as batch:
        batch.drop_index("ix_order_tenant_staff_paid")
        batch.drop_index("ix_orders_staff_id")
        batch.drop_constraint("uq_orders_reservation_id", type_="unique")
        batch.drop_constraint("fk_orders_staff_id", type_="foreignkey")
        batch.drop_constraint("fk_orders_reservation_id", type_="foreignkey")
        batch.drop_column("tip_cents")
        batch.drop_column("payment_method")
        batch.drop_column("staff_id")
        batch.drop_column("reservation_id")
        batch.drop_column("points_cents")
