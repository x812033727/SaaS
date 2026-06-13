"""Tenants router — 租戶資訊端點。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from saas_mvp.deps import get_current_user, get_db
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.user import User

router = APIRouter(prefix="/tenants", tags=["tenants"])


class TenantInfo(BaseModel):
    id: int
    name: str
    plan: str

    model_config = {"from_attributes": True}


@router.get("/me", response_model=TenantInfo)
def get_my_tenant(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TenantInfo:
    """回傳當前使用者所屬租戶資訊。"""
    tenant = db.get(Tenant, current_user.tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found",
        )
    return TenantInfo.model_validate(tenant)
