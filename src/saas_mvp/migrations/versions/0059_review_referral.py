"""Review link + referral loop (R11-B).

Revision ID: a7d4e9b2c165
Revises: f3a9c2d7e581

business_profiles.review_url:外部評論連結({review_url} 佔位符來源)。
booking_customers:referral_code(tenant 內唯一)/referred_by/rewarded_at。
tenant_loyalty_configs.referral_points:推薦獎勵點數。

冪等守衛:inspect 後已存在則跳過(比照 0050)。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a7d4e9b2c165"
down_revision = "f3a9c2d7e581"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())

    profile_cols = {c["name"] for c in insp.get_columns("business_profiles")}
    if "review_url" not in profile_cols:
        op.add_column(
            "business_profiles",
            sa.Column("review_url", sa.String(length=512), nullable=True),
        )

    cust_cols = {c["name"] for c in insp.get_columns("booking_customers")}
    if "referral_code" not in cust_cols:
        # batch mode:SQLite 不支援 ALTER 加約束(copy-and-move);PG 照常 ALTER
        with op.batch_alter_table("booking_customers") as batch:
            batch.add_column(
                sa.Column("referral_code", sa.String(length=12), nullable=True)
            )
            batch.add_column(
                sa.Column("referred_by_customer_id", sa.Integer(), nullable=True)
            )
            batch.add_column(
                sa.Column(
                    "referral_rewarded_at", sa.DateTime(timezone=True), nullable=True
                )
            )
            batch.create_foreign_key(
                "fk_customer_referred_by",
                "booking_customers",
                ["referred_by_customer_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch.create_unique_constraint(
                "uq_customer_referral_code", ["tenant_id", "referral_code"]
            )

    loyalty_cols = {c["name"] for c in insp.get_columns("tenant_loyalty_configs")}
    if "referral_points" not in loyalty_cols:
        op.add_column(
            "tenant_loyalty_configs",
            sa.Column(
                "referral_points",
                sa.Integer(),
                nullable=False,
                server_default="50",
            ),
        )


def downgrade() -> None:
    op.drop_column("tenant_loyalty_configs", "referral_points")
    with op.batch_alter_table("booking_customers") as batch:
        batch.drop_constraint("uq_customer_referral_code", type_="unique")
        batch.drop_constraint("fk_customer_referred_by", type_="foreignkey")
        batch.drop_column("referral_rewarded_at")
        batch.drop_column("referred_by_customer_id")
        batch.drop_column("referral_code")
    op.drop_column("business_profiles", "review_url")
