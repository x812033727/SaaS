"""GCal drift detection columns.

Revision ID: c6e2a94f7d38
Revises: b5d1f83a7c62
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c6e2a94f7d38"
down_revision = "b5d1f83a7c62"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    resv_cols = {c["name"] for c in insp.get_columns("booking_reservations")}
    if "gcal_drift_detected_at" not in resv_cols:
        op.add_column(
            "booking_reservations",
            sa.Column("gcal_drift_detected_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "gcal_drift_note" not in resv_cols:
        op.add_column(
            "booking_reservations",
            sa.Column("gcal_drift_note", sa.String(length=255), nullable=True),
        )
    cred_cols = {c["name"] for c in insp.get_columns("tenant_gcal_credentials")}
    if "last_drift_check_at" not in cred_cols:
        op.add_column(
            "tenant_gcal_credentials",
            sa.Column("last_drift_check_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("tenant_gcal_credentials", "last_drift_check_at")
    op.drop_column("booking_reservations", "gcal_drift_note")
    op.drop_column("booking_reservations", "gcal_drift_detected_at")
