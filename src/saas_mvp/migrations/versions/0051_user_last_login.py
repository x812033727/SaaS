"""users: last_login_at / last_login_ip (R5-D1 login audit).

Revision ID: c3e0a6f2d871
Revises: a1c8e4d9f750

冪等守衛:inspect 後已存在則跳過。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c3e0a6f2d871"
down_revision = "a1c8e4d9f750"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("users")}
    if "last_login_at" not in cols:
        op.add_column(
            "users",
            sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "last_login_ip" not in cols:
        op.add_column(
            "users",
            sa.Column("last_login_ip", sa.String(length=64), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("users", "last_login_ip")
    op.drop_column("users", "last_login_at")
