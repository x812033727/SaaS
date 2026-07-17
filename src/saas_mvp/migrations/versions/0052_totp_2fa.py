"""TOTP 2FA: users totp columns + recovery codes table (R5-D2).

Revision ID: d4f1b7a3e982
Revises: c3e0a6f2d871

冪等守衛:inspect 後已存在則跳過。
⚠️Boolean 預設一律 sa.false()/sa.true(),勿用 sa.text("0")(PG 拒絕,見 0050 事故)。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4f1b7a3e982"
down_revision = "c3e0a6f2d871"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("users")}
    if "totp_secret_enc" not in cols:
        op.add_column(
            "users", sa.Column("totp_secret_enc", sa.LargeBinary(), nullable=True)
        )
    if "totp_enabled_at" not in cols:
        op.add_column(
            "users",
            sa.Column("totp_enabled_at", sa.DateTime(timezone=True), nullable=True),
        )

    if "totp_recovery_codes" not in insp.get_table_names():
        op.create_table(
            "totp_recovery_codes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("code_hash", sa.String(length=64), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_totp_recovery_codes_user_id", "totp_recovery_codes", ["user_id"]
        )


def downgrade() -> None:
    op.drop_index("ix_totp_recovery_codes_user_id", table_name="totp_recovery_codes")
    op.drop_table("totp_recovery_codes")
    op.drop_column("users", "totp_enabled_at")
    op.drop_column("users", "totp_secret_enc")
