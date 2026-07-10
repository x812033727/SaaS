"""Quota router — 查詢目前租戶 API 用量狀態。"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from saas_mvp.deps import get_current_user, get_db
from saas_mvp.models.user import User
from saas_mvp.quota import get_quota_status
from saas_mvp.services.ai_quota import get_ai_quota_status
from saas_mvp.services.push_quota import get_push_quota_status

router = APIRouter(prefix="/quota", tags=["quota"])


@router.get("/status")
def quota_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """回傳當前使用者所屬租戶今日 API 配額狀態。"""
    tenant = current_user.tenant
    return get_quota_status(db, tenant.id, tenant.plan)


@router.get("/push")
def push_quota_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """回傳當前租戶本月推播額度狀態。

    欄位：period（YYYYMM）/ used / allowance / remaining / boost_enabled。
    額度 = push_allowance_base（+ push_allowance_boost 若開通 PUSH_BOOST）。
    """
    return get_push_quota_status(db, current_user.tenant_id)


@router.get("/ai")
def ai_quota_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """回傳當前租戶本月 AI 對話額度狀態（A2.4）。

    欄位同 /quota/push；額度 = ai_allowance_base（+ ai_allowance_boost
    若明確訂閱 AI_BOOST）。
    """
    return get_ai_quota_status(db, current_user.tenant_id)
