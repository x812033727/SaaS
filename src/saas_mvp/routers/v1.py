"""Versioned API foundation for the Next.js application."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from saas_mvp.auth.dependencies import get_current_user
from saas_mvp.db import get_db
from saas_mvp.models.organization import Organization, OrganizationMember
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.user import User
from saas_mvp.services import organizations as organizations_svc

router = APIRouter(prefix="/api/v1", tags=["v1"])


class UserContext(BaseModel):
    id: int
    email: str


class TenantContext(BaseModel):
    id: int
    name: str
    role: str


class OrganizationContext(BaseModel):
    id: int
    name: str
    slug: str
    role: str
    share_customers: bool
    share_loyalty: bool
    share_coupons: bool


class AppContext(BaseModel):
    user: UserContext
    organization: OrganizationContext
    tenant: TenantContext
    permissions: list[str]


class OrganizationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    share_customers: bool | None = None
    share_loyalty: bool | None = None
    share_coupons: bool | None = None


class MemberRow(BaseModel):
    user_id: int
    email: str
    role: str
    is_active: bool


def _memberships(db: Session, current_user: User):
    org_member = organizations_svc.get_user_organization(db, user_id=current_user.id)
    tenant_member = organizations_svc.get_user_tenant(
        db, user_id=current_user.id, tenant_id=current_user.tenant_id
    )
    organization = db.get(Organization, org_member.organization_id)
    tenant = db.get(Tenant, current_user.tenant_id)
    if organization is None or tenant is None:
        # Membership foreign keys normally make this impossible. Reuse the same
        # non-enumerating response used by the membership helpers.
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid active context"
        )
    return organization, tenant, org_member, tenant_member


@router.get("/context", response_model=AppContext)
def get_context(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AppContext:
    organization, tenant, org_member, tenant_member = _memberships(db, current_user)
    return AppContext(
        user=UserContext(id=current_user.id, email=current_user.email),
        organization=OrganizationContext(
            id=organization.id,
            name=organization.name,
            slug=organization.slug,
            role=org_member.role,
            share_customers=organization.share_customers,
            share_loyalty=organization.share_loyalty,
            share_coupons=organization.share_coupons,
        ),
        tenant=TenantContext(id=tenant.id, name=tenant.name, role=tenant_member.role),
        permissions=organizations_svc.permissions_for(
            org_member.role, tenant_member.role
        ),
    )


@router.patch("/organization", response_model=OrganizationContext)
def update_organization(
    body: OrganizationUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OrganizationContext:
    organization, _, org_member, _ = _memberships(db, current_user)
    organizations_svc.require_permission(org_member, "organization:manage")
    for field in body.model_fields_set:
        value = getattr(body, field)
        if field == "name" and value is not None:
            value = value.strip()
        setattr(organization, field, value)
    db.commit()
    db.refresh(organization)
    return OrganizationContext(
        id=organization.id,
        name=organization.name,
        slug=organization.slug,
        role=org_member.role,
        share_customers=organization.share_customers,
        share_loyalty=organization.share_loyalty,
        share_coupons=organization.share_coupons,
    )


@router.get("/organization/members", response_model=list[MemberRow])
def list_organization_members(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[MemberRow]:
    organization, _, org_member, _ = _memberships(db, current_user)
    organizations_svc.require_permission(org_member, "members:manage")
    rows = db.execute(
        select(OrganizationMember)
        .options(joinedload(OrganizationMember.user))
        .where(OrganizationMember.organization_id == organization.id)
        .order_by(OrganizationMember.id)
    ).scalars()
    return [
        MemberRow(
            user_id=row.user_id,
            email=row.user.email,
            role=row.role,
            is_active=row.is_active,
        )
        for row in rows
    ]
