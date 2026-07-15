"""Add encrypted platform payment configuration.

Revision ID: d9e71a5c4302
Revises: b6a39e2c8421
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d9e71a5c4302"
down_revision: Union[str, None] = "b6a39e2c8421"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if "platform_payment_configs" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "platform_payment_configs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("merchant_id", sa.String(64), nullable=False),
        sa.Column("hash_key_enc", sa.LargeBinary(), nullable=False),
        sa.Column("hash_iv_enc", sa.LargeBinary(), nullable=False),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("platform_payment_configs")
