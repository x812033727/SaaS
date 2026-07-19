"""Email-only rows in reminder/notify queues (R12-B).

Revision ID: c2f6a9d4e158
Revises: b8e5f1a3d947

booking_reservation_reminders.line_user_id / booking_notifications.line_user_id
放寬為 nullable:NULL = 無 LINE 身分的 email-only 收件人(網路預約客),
派送端據此路由 email(可靠佇列)而非 LINE push。

SQLite 不支援 ALTER COLUMN → batch_alter_table(copy-and-move);PG 為
一般 ALTER。冪等:已 nullable 則跳過。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c2f6a9d4e158"
down_revision = "b8e5f1a3d947"
branch_labels = None
depends_on = None

_TABLES = ("booking_reservation_reminders", "booking_notifications")


def _is_nullable(table: str) -> bool:
    insp = sa.inspect(op.get_bind())
    for col in insp.get_columns(table):
        if col["name"] == "line_user_id":
            return bool(col["nullable"])
    raise RuntimeError(f"{table}.line_user_id not found")


def upgrade() -> None:
    for table in _TABLES:
        if _is_nullable(table):
            continue
        with op.batch_alter_table(table) as batch:
            batch.alter_column(
                "line_user_id", existing_type=sa.String(64), nullable=True
            )


def downgrade() -> None:
    for table in _TABLES:
        if not _is_nullable(table):
            continue
        op.execute(
            sa.text(f"DELETE FROM {table} WHERE line_user_id IS NULL")  # noqa: S608
        )
        with op.batch_alter_table(table) as batch:
            batch.alter_column(
                "line_user_id", existing_type=sa.String(64), nullable=False
            )
