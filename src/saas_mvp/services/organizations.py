"""Organization creation, membership and permission helpers."""

from __future__ import annotations

import re
import secrets

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.models.organization import Organization, OrganizationMember, TenantMember

ORG_ROLES = frozenset({"owner", "admin", "accountant", "marketer", "viewer"})
TENANT_ROLES = frozenset(
    {"owner", "admin", "manager", "staff", "accountant", "marketer", "viewer"}
)

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "owner": frozenset(
        {
            "organization:manage",
            "billing:manage",
            "members:manage",
            "tenant:manage",
            "operations:write",
            "reports:read",
        }
    ),
    "admin": frozenset(
        {"members:manage", "tenant:manage", "operations:write", "reports:read"}
    ),
    "manager": frozenset({"tenant:manage", "operations:write", "reports:read"}),
    "staff": frozenset({"operations:write"}),
    "accountant": frozenset({"billing:manage", "reports:read"}),
    "marketer": frozenset({"marketing:manage", "reports:read"}),
    "viewer": frozenset({"reports:read"}),
}


def _slug_base(name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return value[:48] or "organization"


def create_organization(db: Session, *, name: str, flush: bool = False) -> Organization:
    base = _slug_base(name)
    slug = base
    while db.execute(select(Organization.id).where(Organization.slug == slug)).first():
        slug = f"{base[:40]}-{secrets.token_hex(3)}"
    organization = Organization(name=name.strip(), slug=slug)
    db.add(organization)
    if flush:
        db.flush()
    return organization


def add_owner_memberships(
    db: Session, *, organization_id: int, tenant_id: int, user_id: int
) -> None:
    db.add(
        OrganizationMember(
            organization_id=organization_id, user_id=user_id, role="owner"
        )
    )
    db.add(TenantMember(tenant_id=tenant_id, user_id=user_id, role="owner"))


def ensure_user_memberships(db: Session, *, tenant, user) -> Organization:
    """Ensure legacy/admin/invite creation paths receive scoped memberships.

    The helper deliberately does not commit so callers can keep user, tenant and
    membership provisioning in one transaction.
    """
    if user.id is None:
        db.flush()
    organization = None
    if tenant.organization_id is not None:
        organization = db.get(Organization, tenant.organization_id)
    if organization is None:
        organization = create_organization(db, name=tenant.name, flush=True)
        tenant.organization_id = organization.id
        db.add(tenant)

    tenant_role = user.role if user.role in TENANT_ROLES else "viewer"
    org_role = "owner" if tenant_role == "owner" else "viewer"
    org_member = db.execute(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == organization.id,
            OrganizationMember.user_id == user.id,
        )
    ).scalar_one_or_none()
    if org_member is None:
        db.add(
            OrganizationMember(
                organization_id=organization.id, user_id=user.id, role=org_role
            )
        )
    tenant_member = db.execute(
        select(TenantMember).where(
            TenantMember.tenant_id == tenant.id,
            TenantMember.user_id == user.id,
        )
    ).scalar_one_or_none()
    if tenant_member is None:
        db.add(TenantMember(tenant_id=tenant.id, user_id=user.id, role=tenant_role))
    return organization


def get_user_organization(
    db: Session, *, user_id: int, organization_id: int | None = None
) -> OrganizationMember:
    stmt = select(OrganizationMember).where(
        OrganizationMember.user_id == user_id,
        OrganizationMember.is_active.is_(True),
    )
    if organization_id is not None:
        stmt = stmt.where(OrganizationMember.organization_id == organization_id)
    membership = db.execute(stmt.order_by(OrganizationMember.id)).scalars().first()
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No active organization membership",
        )
    return membership


def get_user_tenant(db: Session, *, user_id: int, tenant_id: int) -> TenantMember:
    membership = db.execute(
        select(TenantMember).where(
            TenantMember.user_id == user_id,
            TenantMember.tenant_id == tenant_id,
            TenantMember.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No active tenant membership",
        )
    return membership


def permissions_for(*roles: str) -> list[str]:
    permissions: set[str] = set()
    for role in roles:
        permissions.update(ROLE_PERMISSIONS.get(role, ()))
    return sorted(permissions)


def require_permission(membership: OrganizationMember, permission: str) -> None:
    if permission not in ROLE_PERMISSIONS.get(membership.role, frozenset()):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {permission}",
        )
