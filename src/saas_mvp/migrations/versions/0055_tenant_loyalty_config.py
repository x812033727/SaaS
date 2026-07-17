"""Tenant loyalty config (R6-B3): configurable tiers / discounts / earn rate.

Revision ID: c9f1a4b6e802
Revises: b6d2f8a1c493

tenant_loyalty_configs:per-tenant 會員分級門檻、各級結帳折扣、每筆預約集點數。
無設定 = 沿用全域 settings 預設(向後相容)。

冪等守衛:比照 0050 — inspect 後已存在則跳過。⚠️Boolean 預設用 sa.false()。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c9f1a4b6e802"
down_revision = "b6d2f8a1c493"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "tenant_loyalty_configs" not in insp.get_table_names():
        op.create_table(
            "tenant_loyalty_configs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.Integer(),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("silver_threshold", sa.Integer(), nullable=False, server_default="100"),
            sa.Column("gold_threshold", sa.Integer(), nullable=False, server_default="500"),
            sa.Column("regular_discount_pct", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("silver_discount_pct", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("gold_discount_pct", sa.Integer(), nullable=False, server_default="10"),
            sa.Column("points_per_booking", sa.Integer(), nullable=False, server_default="10"),
            sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_tenant_loyalty_configs_tenant_id",
            "tenant_loyalty_configs",
            ["tenant_id"],
            unique=True,
        )


def downgrade() -> None:
    op.drop_index(
        "ix_tenant_loyalty_configs_tenant_id", table_name="tenant_loyalty_configs"
    )
    op.drop_table("tenant_loyalty_configs")
