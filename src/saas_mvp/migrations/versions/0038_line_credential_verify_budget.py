"""Add per-tenant LINE credential verification budget.

Revision ID: c38f1d72a4b6
Revises: b27e4c91f8a3
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c38f1d72a4b6"
down_revision = "b27e4c91f8a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("line_channel_configs")
    }
    if "verify_attempt_count" not in columns:
        op.add_column(
            "line_channel_configs",
            sa.Column(
                "verify_attempt_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
    if "verify_attempt_window_start" not in columns:
        op.add_column(
            "line_channel_configs",
            sa.Column("verify_attempt_window_start", sa.DateTime(timezone=True)),
        )


def downgrade() -> None:
    op.drop_column("line_channel_configs", "verify_attempt_window_start")
    op.drop_column("line_channel_configs", "verify_attempt_count")
