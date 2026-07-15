"""Add encrypted platform OAuth configuration.

Revision ID: a6d4f891c203
Revises: 9b24e7c1d6a0
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a6d4f891c203"
down_revision: Union[str, None] = "9b24e7c1d6a0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "platform_oauth_configs" in inspector.get_table_names():
        return
    op.create_table(
        "platform_oauth_configs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("client_id", sa.String(length=255), nullable=False),
        sa.Column("client_secret_enc", sa.LargeBinary(), nullable=False),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", name="uq_platform_oauth_provider"),
    )
    op.create_index(
        "ix_platform_oauth_configs_provider",
        "platform_oauth_configs",
        ["provider"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_platform_oauth_configs_provider", table_name="platform_oauth_configs"
    )
    op.drop_table("platform_oauth_configs")
