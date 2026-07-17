"""Order refund state + provider capture (R6-A3).

Revision ID: d1b7e3c8a940
Revises: c9f1a4b6e802

orders 加退款狀態機欄(比照 reservation 定金退款)+ 付款 provider 快照
(退款需原交易的 provider / TradeNo / MerchantID)。

冪等守衛:比照 0050 — inspect 後已存在則跳過。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d1b7e3c8a940"
down_revision = "c9f1a4b6e802"
branch_labels = None
depends_on = None

_COLS = {
    # 付款 provider 快照(退款用;linepay 另用既有 payment_txn_id)。
    "payment_provider": sa.Column("payment_provider", sa.String(length=16), nullable=True),
    "provider_trade_no": sa.Column("provider_trade_no", sa.String(length=20), nullable=True),
    "provider_merchant_id": sa.Column("provider_merchant_id", sa.String(length=64), nullable=True),
    # 退款狀態機(NULL=未申請 | processing | refunded | partially_refunded | failed | manual_required)。
    "refund_status": sa.Column("refund_status", sa.String(length=24), nullable=True),
    "refunded_cents": sa.Column(
        "refunded_cents", sa.Integer(), nullable=False, server_default="0"
    ),
    "refund_provider_code": sa.Column("refund_provider_code", sa.String(length=32), nullable=True),
    "refund_error": sa.Column("refund_error", sa.String(length=255), nullable=True),
    "refund_attempts": sa.Column(
        "refund_attempts", sa.Integer(), nullable=False, server_default="0"
    ),
    "refund_requested_at": sa.Column("refund_requested_at", sa.DateTime(timezone=True), nullable=True),
    "refund_requested_by_user_id": sa.Column("refund_requested_by_user_id", sa.Integer(), nullable=True),
    "refunded_at": sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
}


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    existing = {c["name"] for c in insp.get_columns("orders")}
    for name, col in _COLS.items():
        if name not in existing:
            op.add_column("orders", col)


def downgrade() -> None:
    for name in reversed(list(_COLS)):
        op.drop_column("orders", name)
