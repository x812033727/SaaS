"""Booking notification occurrence: allow multiple same-kind notifications.

Revision ID: b5d1f83a7c62
Revises: a4c7e91b6d20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b5d1f83a7c62"
down_revision = "a4c7e91b6d20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("booking_notifications")}
    # SQLite 改唯一約束需 batch_alter_table(重建表);PG 亦可走同一路徑。
    with op.batch_alter_table("booking_notifications", schema=None) as batch:
        if "occurrence" not in cols:
            batch.add_column(
                sa.Column("occurrence", sa.Integer(), nullable=False, server_default="1")
            )
        batch.drop_constraint("uq_booking_notification", type_="unique")
        batch.create_unique_constraint(
            "uq_booking_notification", ["reservation_id", "kind", "occurrence"]
        )


def downgrade() -> None:
    with op.batch_alter_table("booking_notifications", schema=None) as batch:
        batch.drop_constraint("uq_booking_notification", type_="unique")
        batch.create_unique_constraint(
            "uq_booking_notification", ["reservation_id", "kind"]
        )
        batch.drop_column("occurrence")
