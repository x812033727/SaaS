"""webhook 重放冪等:預約記錄來源 webhook 事件 id(A0.2)。

booking_reservations 加 source_webhook_event_id + (tenant_id, 該欄) 唯一索引,
讓 webhook 重放時 book_slot 能查得既有預約直接回傳,不重複建單。
NULL 不受唯一限制(多筆 NULL 合法),故非 LINE 來源建單不受影響。

Revision ID: e1a7c3d95f24
Revises: c4f8a25d7b19
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'e1a7c3d95f24'
down_revision: Union[str, None] = 'c4f8a25d7b19'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "booking_reservations"
_INDEX = "uq_reservation_source_webhook_event"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    if "source_webhook_event_id" not in cols:
        op.add_column(
            _TABLE,
            sa.Column("source_webhook_event_id", sa.String(length=64), nullable=True),
        )
    # 唯一索引(而非 table constraint)以相容 SQLite ALTER 限制與 Postgres。
    indexes = {i["name"] for i in sa.inspect(bind).get_indexes(_TABLE)}
    if _INDEX not in indexes:
        op.create_index(
            _INDEX,
            _TABLE,
            ["tenant_id", "source_webhook_event_id"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {i["name"] for i in inspector.get_indexes(_TABLE)}
    if _INDEX in indexes:
        op.drop_index(_INDEX, table_name=_TABLE)
    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    if "source_webhook_event_id" in cols:
        op.drop_column(_TABLE, "source_webhook_event_id")
