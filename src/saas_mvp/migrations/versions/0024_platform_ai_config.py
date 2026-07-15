"""Add encrypted platform AI provider configuration.

Revision ID: b6a39e2c8421
Revises: 56fe2d61b93a
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b6a39e2c8421"
down_revision: Union[str, None] = "56fe2d61b93a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if "platform_ai_configs" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "platform_ai_configs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False, unique=True),
        sa.Column("api_key_enc", sa.LargeBinary(), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("platform_ai_configs")
