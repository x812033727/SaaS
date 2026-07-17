"""users: token_version + disabled_at (R5-D3 session revocation + member mgmt).

Revision ID: e7a2c9b4a031
Revises: d4f1b7a3e982

- token_version:JWT `tv` claim 比對來源;改密碼/停用/登出全部 = +1 撤銷既有票。
  Integer server_default 0(PG 對 int 預設無型別問題,與 0050 Boolean 坑不同)。
- disabled_at:成員停用(登入擋+既有票失效,靠 decode 每請求重載 user 即時生效)。

冪等守衛:inspect 後已存在則跳過。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e7a2c9b4a031"
down_revision = "d4f1b7a3e982"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("users")}
    if "token_version" not in cols:
        op.add_column(
            "users",
            sa.Column(
                "token_version",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
    if "disabled_at" not in cols:
        op.add_column(
            "users",
            sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("users", "disabled_at")
    op.drop_column("users", "token_version")
