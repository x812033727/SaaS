"""Add encrypted platform observability configuration.

Revision ID: d39a2c83b5d7
Revises: c38f1d72a4b6
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d39a2c83b5d7"
down_revision = "c38f1d72a4b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "platform_observability_configs" not in set(
        sa.inspect(op.get_bind()).get_table_names()
    ):
        op.create_table(
            "platform_observability_configs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("sentry_dsn_enc", sa.LargeBinary(), nullable=False),
            sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("platform_observability_configs")
