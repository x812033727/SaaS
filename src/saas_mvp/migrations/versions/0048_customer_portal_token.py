"""Customer portal token (R5-B1).

Revision ID: e8a4c1f6b527
Revises: d7f3b0e5a419

booking_customers.portal_token:顧客自助入口網「我的預約」長效憑證
(token 即能力,NULL=尚未產生,惰性簽發比照 ics_token)。

冪等守衛:比照 0005 — inspect 後已存在同名欄位則跳過。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e8a4c1f6b527"
down_revision = "d7f3b0e5a419"
branch_labels = None
depends_on = None

_TABLE = "booking_customers"
_INDEX = "ix_booking_customers_portal_token"


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns(_TABLE)}
    if "portal_token" not in cols:
        op.add_column(
            _TABLE,
            sa.Column("portal_token", sa.String(length=64), nullable=True),
        )
    index_names = {ix["name"] for ix in insp.get_indexes(_TABLE)}
    if _INDEX not in index_names:
        op.create_index(_INDEX, _TABLE, ["portal_token"], unique=True)


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, "portal_token")
