"""Add tenant invoice profiles and immutable invoice buyer snapshots.

Revision ID: 9b1f6a2e4c80
Revises: 78c4de91a2f6
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "9b1f6a2e4c80"
down_revision: Union[str, None] = "78c4de91a2f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "tenant_invoice_profiles" not in inspector.get_table_names():
        op.create_table(
            "tenant_invoice_profiles",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("tenant_id", sa.Integer(), nullable=False),
            sa.Column("mode", sa.String(16), nullable=False, server_default="personal"),
            sa.Column("buyer_name", sa.String(60), nullable=False, server_default=""),
            sa.Column(
                "buyer_identifier", sa.String(8), nullable=False, server_default=""
            ),
            sa.Column(
                "carrier_type", sa.String(16), nullable=False, server_default="ecpay"
            ),
            sa.Column("carrier_number_enc", sa.LargeBinary(), nullable=True),
            sa.Column("donation_code", sa.String(7), nullable=False, server_default=""),
            sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_tenant_invoice_profiles_tenant_id",
            "tenant_invoice_profiles",
            ["tenant_id"],
            unique=True,
        )

    columns = {column["name"] for column in sa.inspect(bind).get_columns("invoices")}
    additions = {
        "invoice_mode": sa.Column(
            "invoice_mode", sa.String(16), nullable=False, server_default="personal"
        ),
        "buyer_name": sa.Column(
            "buyer_name", sa.String(60), nullable=False, server_default=""
        ),
        "buyer_identifier": sa.Column(
            "buyer_identifier", sa.String(8), nullable=False, server_default=""
        ),
        "carrier_type": sa.Column(
            "carrier_type", sa.String(16), nullable=False, server_default="ecpay"
        ),
        "carrier_number_enc": sa.Column(
            "carrier_number_enc", sa.LargeBinary(), nullable=True
        ),
        "donation_code": sa.Column(
            "donation_code", sa.String(7), nullable=False, server_default=""
        ),
    }
    for name, column in additions.items():
        if name not in columns:
            op.add_column("invoices", column)


def downgrade() -> None:
    for name in (
        "donation_code",
        "carrier_number_enc",
        "carrier_type",
        "buyer_identifier",
        "buyer_name",
        "invoice_mode",
    ):
        op.drop_column("invoices", name)
    op.drop_index(
        "ix_tenant_invoice_profiles_tenant_id",
        table_name="tenant_invoice_profiles",
    )
    op.drop_table("tenant_invoice_profiles")
