"""Customer email (R5-B3).

Revision ID: f9b5d2c7a638
Revises: e8a4c1f6b527

booking_customers.email:顧客 email(選填)——提醒三段 fallback
(LINE→SMS→email)的第三管道 + booking_form/portal 自助填寫。

冪等守衛:inspect 後已存在同名欄位則跳過。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f9b5d2c7a638"
down_revision = "e8a4c1f6b527"
branch_labels = None
depends_on = None

_TABLE = "booking_customers"


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns(_TABLE)}
    if "email" not in cols:
        op.add_column(
            _TABLE, sa.Column("email", sa.String(length=255), nullable=True)
        )


def downgrade() -> None:
    op.drop_column(_TABLE, "email")
