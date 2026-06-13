"""Quota router — 查詢目前租戶 API 用量狀態。"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from saas_mvp.deps import get_current_user, get_db
from saas_mvp.models.user import User
from saas_mvp.quota import get_quota_status

router = APIRouter(prefix="/quota", tags=["quota"])


@router.get("/status")
def quota_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """回傳當前使用者所屬租戶今日 API 配額狀態。"""
    tenant = current_user.tenant
    return get_quota_status(db, tenant.id, tenant.plan)
