"""Customer marketing opt-out + unsubscribe token (R6-B1, PDPA).

Revision ID: b6d2f8a1c493
Revises: e7a2c9b4a031

booking_customers:
- marketing_opt_out_at:非 NULL = 已退訂行銷推播(交易性通知不受影響);
  既有顧客 NULL = 視為訂閱中(opt-out 模型)。
- unsubscribe_token:退訂連結能力憑證(token 即能力,NULL=惰性尚未產生,
  比照 portal_token/ics_token)。unique index。

冪等守衛:比照 0048 — inspect 後已存在則跳過。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b6d2f8a1c493"
down_revision = "e7a2c9b4a031"
branch_labels = None
depends_on = None

_TABLE = "booking_customers"
_INDEX = "ix_booking_customers_unsubscribe_token"


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns(_TABLE)}
    if "marketing_opt_out_at" not in cols:
        op.add_column(
            _TABLE,
            sa.Column("marketing_opt_out_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "unsubscribe_token" not in cols:
        op.add_column(
            _TABLE,
            sa.Column("unsubscribe_token", sa.String(length=64), nullable=True),
        )
    index_names = {ix["name"] for ix in insp.get_indexes(_TABLE)}
    if _INDEX not in index_names:
        op.create_index(_INDEX, _TABLE, ["unsubscribe_token"], unique=True)


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, "unsubscribe_token")
    op.drop_column(_TABLE, "marketing_opt_out_at")
