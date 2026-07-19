"""Public standing online booking opt-in (R12-A).

Revision ID: b8e5f1a3d947
Revises: a7d4e9b2c165

business_profiles.online_booking_enabled:公開頁 /p/{slug}/book 常駐預約
入口的店家 opt-in 旗標(另需 WEB_BOOKING feature + is_published)。

冪等守衛:inspect 後已存在則跳過(比照 0059)。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b8e5f1a3d947"
down_revision = "a7d4e9b2c165"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    if not _has_column("business_profiles", "online_booking_enabled"):
        op.add_column(
            "business_profiles",
            sa.Column(
                "online_booking_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )


def downgrade() -> None:
    if _has_column("business_profiles", "online_booking_enabled"):
        op.drop_column("business_profiles", "online_booking_enabled")
