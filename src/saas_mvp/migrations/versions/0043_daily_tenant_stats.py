"""Add daily_tenant_stats pre-aggregation table.

Revision ID: f8c2d95e7b31
Revises: e2d8b74f6a91
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f8c2d95e7b31"
down_revision = "e2d8b74f6a91"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "daily_tenant_stats" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "daily_tenant_stats",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Integer(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stat_date", sa.Date(), nullable=False),
        sa.Column("bookings_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bookings_confirmed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bookings_cancelled", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("covers", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("distinct_customers", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attended", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("no_show", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("paid_orders", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revenue_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "stat_date", name="uq_daily_stat_tenant_date"),
    )
    op.create_index(
        "ix_daily_tenant_stats_tenant_id", "daily_tenant_stats", ["tenant_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_daily_tenant_stats_tenant_id", table_name="daily_tenant_stats")
    op.drop_table("daily_tenant_stats")
