"""Gift card online sales (R11-A): tenant config + purchase rows.

Revision ID: f3a9c2d7e581
Revises: e2c4a8f1b063

tenant_gift_card_configs:每租戶線上販售開關/面額清單/履約保障文案。
gift_card_purchases:一次購買=一張 Order(order_id 唯一);付款成功
callback 同交易發卡,明碼 Fernet 加密存 code_enc 供成功頁/交付信。

冪等守衛:比照 0050 — inspect 後已存在則跳過。Boolean server_default
用 sa.false()(R5-0050 事故鐵律)。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f3a9c2d7e581"
down_revision = "e2c4a8f1b063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = insp.get_table_names()
    if "tenant_gift_card_configs" not in tables:
        op.create_table(
            "tenant_gift_card_configs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.Integer(),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
                unique=True,
            ),
            sa.Column(
                "online_sale_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column("denominations", sa.Text(), nullable=True),
            sa.Column("fulfillment_guarantee", sa.Text(), nullable=True),
            sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "gift_card_purchases" not in tables:
        op.create_table(
            "gift_card_purchases",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.Integer(),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "order_id",
                sa.Integer(),
                sa.ForeignKey("orders.id", ondelete="RESTRICT"),
                nullable=False,
                unique=True,
            ),
            sa.Column(
                "gift_card_id",
                sa.Integer(),
                sa.ForeignKey("gift_cards.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("amount_cents", sa.Integer(), nullable=False),
            sa.Column("purchaser_name", sa.String(length=128), nullable=True),
            sa.Column("purchaser_email", sa.String(length=256), nullable=False),
            sa.Column("recipient_name", sa.String(length=128), nullable=True),
            sa.Column("message", sa.String(length=500), nullable=True),
            sa.Column(
                "status",
                sa.String(length=16),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("code_enc", sa.LargeBinary(), nullable=True),
            sa.Column("email_queued_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("gift_card_purchases")
    op.drop_table("tenant_gift_card_configs")
