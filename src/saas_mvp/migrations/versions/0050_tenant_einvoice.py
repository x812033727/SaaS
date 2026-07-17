"""Tenant e-invoice config + order/deposit invoice links (R5-C2).

Revision ID: a1c8e4d9f750
Revises: f9b5d2c7a638

- tenant_einvoice_configs:店家自有綠界發票憑證(Fernet 加密,opt-in)。
- invoices.reservation_id:定金發票回鏈(order_id 已為既有預留欄)。

冪等守衛:inspect 後已存在則跳過。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a1c8e4d9f750"
down_revision = "f9b5d2c7a638"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "tenant_einvoice_configs" not in insp.get_table_names():
        op.create_table(
            "tenant_einvoice_configs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.Integer(),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "merchant_id", sa.String(length=20), nullable=False,
                server_default="",
            ),
            sa.Column("hash_key_enc", sa.LargeBinary(), nullable=True),
            sa.Column("hash_iv_enc", sa.LargeBinary(), nullable=True),
            sa.Column(
                "environment", sa.String(length=8), nullable=False,
                server_default="stage",
            ),
            sa.Column(
                "enabled", sa.Boolean(), nullable=False,
                # sa.false():PG 的 boolean 不接受 DEFAULT 0(型別不匹配,
                # SQLite 才容忍)——此 bug 曾讓 prod 起不來,勿再用 sa.text("0")。
                server_default=sa.false(),
            ),
            sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_tenant_einvoice_configs_tenant_id",
            "tenant_einvoice_configs",
            ["tenant_id"],
            unique=True,
        )

    inv_cols = {c["name"] for c in insp.get_columns("invoices")}
    if "reservation_id" not in inv_cols:
        op.add_column(
            "invoices",
            sa.Column("reservation_id", sa.Integer(), nullable=True),
        )
        op.create_index(
            "ix_invoices_reservation_id", "invoices", ["reservation_id"]
        )


def downgrade() -> None:
    op.drop_index("ix_invoices_reservation_id", table_name="invoices")
    op.drop_column("invoices", "reservation_id")
    op.drop_index(
        "ix_tenant_einvoice_configs_tenant_id",
        table_name="tenant_einvoice_configs",
    )
    op.drop_table("tenant_einvoice_configs")
