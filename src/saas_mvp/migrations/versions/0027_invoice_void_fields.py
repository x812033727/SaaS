"""Add invoice void operation fields.

Revision ID: 78c4de91a2f6
Revises: 4f3b2a91d8c7
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "78c4de91a2f6"
down_revision: Union[str, None] = "4f3b2a91d8c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("invoices")
    }
    if "void_reason" not in columns:
        op.add_column("invoices", sa.Column("void_reason", sa.String(20), nullable=True))
    if "void_error_msg" not in columns:
        op.add_column(
            "invoices", sa.Column("void_error_msg", sa.String(255), nullable=True)
        )
    if "voided_at" not in columns:
        op.add_column(
            "invoices", sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    op.drop_column("invoices", "voided_at")
    op.drop_column("invoices", "void_error_msg")
    op.drop_column("invoices", "void_reason")
