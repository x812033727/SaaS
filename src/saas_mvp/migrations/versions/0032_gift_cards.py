"""Add commercial gift cards and balance ledger.

Revision ID: a8d29c4ef731
Revises: f6b1d4208a31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a8d29c4ef731"
down_revision = "f6b1d4208a31"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    expected = {"gift_cards", "gift_card_ledger"}
    order_columns = {c["name"] for c in inspector.get_columns("orders")}
    if expected <= tables and "gift_card_cents" in order_columns:
        return
    partial = tables & expected
    if partial or ("gift_card_cents" in order_columns):
        raise RuntimeError("partial gift card schema; manual recovery required")

    op.add_column("orders", sa.Column("gift_card_cents", sa.Integer(), nullable=False, server_default="0"))
    op.create_table(
        "gift_cards",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("code_last4", sa.String(4), nullable=False),
        sa.Column("recipient_customer_id", sa.Integer(), nullable=True),
        sa.Column("initial_value_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("purchaser_name", sa.String(128), nullable=True),
        sa.Column("recipient_name", sa.String(128), nullable=True),
        sa.Column("message", sa.String(500), nullable=True),
        sa.Column("fulfillment_guarantee", sa.Text(), nullable=False),
        sa.Column("issuance_key", sa.String(64), nullable=False),
        sa.Column("issued_by_user_id", sa.Integer(), nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recipient_customer_id"], ["booking_customers.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("tenant_id", "code_hash", name="uq_gift_card_code_hash"),
        sa.UniqueConstraint("tenant_id", "issuance_key", name="uq_gift_card_issuance_key"),
    )
    op.create_index("ix_gift_cards_tenant_id", "gift_cards", ["tenant_id"])
    op.create_index("ix_gift_cards_recipient_customer_id", "gift_cards", ["recipient_customer_id"])
    op.create_index("ix_gift_card_tenant_recipient_status", "gift_cards", ["tenant_id", "recipient_customer_id", "status"])
    op.create_table(
        "gift_card_ledger",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("gift_card_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("delta_cents", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("note", sa.String(255), nullable=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["gift_card_id"], ["gift_cards.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["customer_id"], ["booking_customers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("tenant_id", "order_id", "kind", name="uq_gift_card_order_kind"),
    )
    for column in ("tenant_id", "gift_card_id", "customer_id", "order_id"):
        op.create_index(f"ix_gift_card_ledger_{column}", "gift_card_ledger", [column])
    op.create_index("ix_gift_card_ledger_balance", "gift_card_ledger", ["tenant_id", "gift_card_id"])


def downgrade() -> None:
    op.drop_table("gift_card_ledger")
    op.drop_table("gift_cards")
    op.drop_column("orders", "gift_card_cents")
