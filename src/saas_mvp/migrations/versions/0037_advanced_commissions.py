"""Add tiered commissions, sales goals and earning audit snapshots.

Revision ID: b27e4c91f8a3
Revises: a91d7c4e2b60
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b27e4c91f8a3"
down_revision = "a91d7c4e2b60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    rule_columns = {
        column["name"] for column in inspector.get_columns("staff_commission_rules")
    }
    earning_columns = {
        column["name"] for column in inspector.get_columns("staff_commission_earnings")
    }

    if "structure" not in rule_columns:
        op.add_column(
            "staff_commission_rules",
            sa.Column(
                "structure", sa.String(16), nullable=False, server_default="fixed"
            ),
        )
    if "sales_period" not in rule_columns:
        op.add_column(
            "staff_commission_rules",
            sa.Column("sales_period", sa.String(16), nullable=True),
        )

    earning_additions = (
        sa.Column(
            "rule_structure_snapshot",
            sa.String(16),
            nullable=False,
            server_default="fixed",
        ),
        sa.Column("sales_period_snapshot", sa.String(16), nullable=True),
        sa.Column(
            "period_sales_before_cents",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("tier_detail_snapshot", sa.Text(), nullable=True),
    )
    for column in earning_additions:
        if column.name not in earning_columns:
            op.add_column("staff_commission_earnings", column)

    if "staff_commission_tiers" not in tables:
        op.create_table(
            "staff_commission_tiers",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), nullable=False),
            sa.Column("rule_id", sa.Integer(), nullable=False),
            sa.Column("threshold_cents", sa.Integer(), nullable=False),
            sa.Column("value", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["rule_id"], ["staff_commission_rules.id"], ondelete="CASCADE"
            ),
            sa.UniqueConstraint(
                "rule_id", "threshold_cents", name="uq_commission_tier_threshold"
            ),
        )
        op.create_index(
            "ix_staff_commission_tiers_tenant_id",
            "staff_commission_tiers",
            ["tenant_id"],
        )
        op.create_index(
            "ix_staff_commission_tiers_rule_id", "staff_commission_tiers", ["rule_id"]
        )

    if "staff_sales_goals" not in tables:
        op.create_table(
            "staff_sales_goals",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), nullable=False),
            sa.Column("staff_id", sa.Integer(), nullable=False),
            sa.Column("item_type", sa.String(16), nullable=False, server_default="all"),
            sa.Column("target_cents", sa.Integer(), nullable=False),
            sa.Column(
                "sales_period", sa.String(16), nullable=False, server_default="monthly"
            ),
            sa.Column("effective_from", sa.Date(), nullable=False),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("created_by_user_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["staff_id"], ["booking_staff.id"], ondelete="CASCADE"
            ),
        )
        op.create_index(
            "ix_staff_sales_goals_tenant_id", "staff_sales_goals", ["tenant_id"]
        )
        op.create_index(
            "ix_staff_sales_goals_staff_id", "staff_sales_goals", ["staff_id"]
        )
        op.create_index(
            "ix_staff_sales_goal_lookup",
            "staff_sales_goals",
            ["tenant_id", "staff_id", "item_type", "effective_from"],
        )


def downgrade() -> None:
    op.drop_table("staff_sales_goals")
    op.drop_table("staff_commission_tiers")
    op.drop_column("staff_commission_earnings", "tier_detail_snapshot")
    op.drop_column("staff_commission_earnings", "period_sales_before_cents")
    op.drop_column("staff_commission_earnings", "sales_period_snapshot")
    op.drop_column("staff_commission_earnings", "rule_structure_snapshot")
    op.drop_column("staff_commission_rules", "sales_period")
    op.drop_column("staff_commission_rules", "structure")
