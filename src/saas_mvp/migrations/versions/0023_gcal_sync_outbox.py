"""Add reliable Google Calendar synchronization outbox.

Revision ID: 56fe2d61b93a
Revises: c8f4b613e725
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "56fe2d61b93a"
down_revision: Union[str, None] = "c8f4b613e725"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if "gcal_sync_jobs" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "gcal_sync_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Integer(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "reservation_id",
            sa.Integer(),
            sa.ForeignKey("booking_reservations.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(255), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_gcal_sync_jobs_tenant_id", "gcal_sync_jobs", ["tenant_id"])
    op.create_index(
        "ix_gcal_sync_job_status_due",
        "gcal_sync_jobs",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_gcal_sync_job_status_due", table_name="gcal_sync_jobs")
    op.drop_index("ix_gcal_sync_jobs_tenant_id", table_name="gcal_sync_jobs")
    op.drop_table("gcal_sync_jobs")
