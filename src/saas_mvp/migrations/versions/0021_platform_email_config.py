"""Add encrypted platform SMTP configuration.

Revision ID: b7e5a902d314
Revises: a6d4f891c203
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7e5a902d314"
down_revision: Union[str, None] = "a6d4f891c203"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if "platform_email_configs" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "platform_email_configs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("smtp_host", sa.String(length=255), nullable=False),
        sa.Column("smtp_port", sa.Integer(), nullable=False),
        sa.Column("smtp_user", sa.String(length=255), nullable=False),
        sa.Column("smtp_password_enc", sa.LargeBinary(), nullable=False),
        sa.Column("smtp_from", sa.String(length=255), nullable=False),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("platform_email_configs")
