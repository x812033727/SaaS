"""Add organization boundary and scoped memberships.

Revision ID: 9b24e7c1d6a0
Revises: e1a7c3d95f24
"""

from __future__ import annotations

import datetime
import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "9b24e7c1d6a0"
down_revision: Union[str, None] = "e1a7c3d95f24"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _slug(name: str, tenant_id: int) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return f"{(base[:48] or 'organization')}-{tenant_id}"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "organizations" not in tables:
        op.create_table(
            "organizations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("slug", sa.String(length=64), nullable=False),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column(
                "share_customers",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column(
                "share_loyalty", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column(
                "share_coupons", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("slug", name="uq_organizations_slug"),
        )
        op.create_index("ix_organizations_slug", "organizations", ["slug"], unique=True)
    tenant_columns = {
        column["name"] for column in sa.inspect(bind).get_columns("tenants")
    }
    if "organization_id" not in tenant_columns:
        op.add_column(
            "tenants", sa.Column("organization_id", sa.Integer(), nullable=True)
        )
        op.create_index("ix_tenants_organization_id", "tenants", ["organization_id"])

    if "organization_members" not in tables:
        op.create_table(
            "organization_members",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "organization_id",
                sa.Integer(),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "role", sa.String(length=24), nullable=False, server_default="viewer"
            ),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "organization_id", "user_id", name="uq_org_member_user"
            ),
        )
        op.create_index(
            "ix_organization_members_organization_id",
            "organization_members",
            ["organization_id"],
        )
        op.create_index(
            "ix_organization_members_user_id", "organization_members", ["user_id"]
        )
    if "tenant_members" not in tables:
        op.create_table(
            "tenant_members",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.Integer(),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "role", sa.String(length=24), nullable=False, server_default="viewer"
            ),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("tenant_id", "user_id", name="uq_tenant_member_user"),
        )
        op.create_index("ix_tenant_members_tenant_id", "tenant_members", ["tenant_id"])
        op.create_index("ix_tenant_members_user_id", "tenant_members", ["user_id"])
    if "location_members" not in tables:
        op.create_table(
            "location_members",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "location_id",
                sa.Integer(),
                sa.ForeignKey("booking_locations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "role", sa.String(length=24), nullable=False, server_default="viewer"
            ),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "location_id", "user_id", name="uq_location_member_user"
            ),
        )
        op.create_index(
            "ix_location_members_location_id", "location_members", ["location_id"]
        )
        op.create_index("ix_location_members_user_id", "location_members", ["user_id"])

    # Backfill makes the migration safe for developer/demo databases even though
    # the first commercial deployment starts from a clean schema.
    now = datetime.datetime.now(datetime.timezone.utc)
    tenants = bind.execute(
        sa.text(
            "SELECT id, name FROM tenants WHERE organization_id IS NULL ORDER BY id"
        )
    ).mappings()
    for tenant in tenants:
        result = bind.execute(
            sa.text(
                "INSERT INTO organizations (name, slug, is_active, share_customers, share_loyalty, share_coupons, created_at, updated_at) VALUES (:name, :slug, true, false, false, false, :now, :now)"
            ),
            {
                "name": tenant["name"],
                "slug": _slug(tenant["name"], tenant["id"]),
                "now": now,
            },
        )
        organization_id = result.lastrowid
        if organization_id is None:
            organization_id = bind.execute(
                sa.text("SELECT id FROM organizations WHERE slug=:slug"),
                {"slug": _slug(tenant["name"], tenant["id"])},
            ).scalar_one()
        bind.execute(
            sa.text("UPDATE tenants SET organization_id=:oid WHERE id=:tid"),
            {"oid": organization_id, "tid": tenant["id"]},
        )
        users = bind.execute(
            sa.text("SELECT id, role FROM users WHERE tenant_id=:tid"),
            {"tid": tenant["id"]},
        ).mappings()
        for user in users:
            tenant_role = (
                user["role"] if user["role"] in {"owner", "staff"} else "viewer"
            )
            org_role = "owner" if tenant_role == "owner" else "viewer"
            bind.execute(
                sa.text(
                    "INSERT INTO organization_members (organization_id, user_id, role, is_active, created_at) VALUES (:oid, :uid, :role, true, :now)"
                ),
                {
                    "oid": organization_id,
                    "uid": user["id"],
                    "role": org_role,
                    "now": now,
                },
            )
            bind.execute(
                sa.text(
                    "INSERT INTO tenant_members (tenant_id, user_id, role, is_active, created_at) VALUES (:tid, :uid, :role, true, :now)"
                ),
                {
                    "tid": tenant["id"],
                    "uid": user["id"],
                    "role": tenant_role,
                    "now": now,
                },
            )

    foreign_keys = sa.inspect(bind).get_foreign_keys("tenants")
    if not any(
        fk.get("constrained_columns") == ["organization_id"] for fk in foreign_keys
    ):
        with op.batch_alter_table("tenants") as batch:
            batch.create_foreign_key(
                "fk_tenants_organization_id",
                "organizations",
                ["organization_id"],
                ["id"],
                ondelete="SET NULL",
            )


def downgrade() -> None:
    op.drop_table("location_members")
    op.drop_table("tenant_members")
    op.drop_table("organization_members")
    with op.batch_alter_table("tenants") as batch:
        batch.drop_constraint("fk_tenants_organization_id", type_="foreignkey")
    op.drop_index("ix_tenants_organization_id", table_name="tenants")
    op.drop_column("tenants", "organization_id")
    op.drop_index("ix_organizations_slug", table_name="organizations")
    op.drop_table("organizations")
