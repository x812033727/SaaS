"""Tenant demo-object tracking (R4-B4).

Revision ID: d7f3b0e5a419
Revises: c6e2a94f7d38
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d7f3b0e5a419"
down_revision = "c6e2a94f7d38"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "tenant_demo_objects" in insp.get_table_names():
        return
    op.create_table(
        "tenant_demo_objects",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Integer(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("object_type", sa.String(length=16), nullable=False),
        sa.Column("object_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_tenant_demo_objects_id", "tenant_demo_objects", ["id"]
    )
    op.create_index(
        "ix_tenant_demo_objects_tenant_id", "tenant_demo_objects", ["tenant_id"]
    )
    op.create_index(
        "ix_tenant_demo_objects_tenant_type",
        "tenant_demo_objects",
        ["tenant_id", "object_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_tenant_demo_objects_tenant_type", "tenant_demo_objects")
    op.drop_index("ix_tenant_demo_objects_tenant_id", "tenant_demo_objects")
    op.drop_index("ix_tenant_demo_objects_id", "tenant_demo_objects")
    op.drop_table("tenant_demo_objects")
