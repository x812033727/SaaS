"""Admin 服務層：租戶列表、用量查詢、租戶 PATCH。

設計決策
--------
* 改方案複用 billing.upgrade_plan / billing.downgrade_plan，不重複邏輯。
* 停用/啟用只寫 Tenant.is_active，不建立 PlanChangeHistory。
* 分頁預設 skip=0, limit=50。
"""

from __future__ import annotations

import datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.models.api_key import ApiKey
from saas_mvp.models.api_key_usage import ApiKeyUsage
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.usage import ApiUsage
from saas_mvp.quota import PLAN_DAILY_LIMITS

# 方案升降判斷順序
_PLAN_ORDER: dict[str, int] = {"free": 0, "pro": 1}


def list_tenants(db: Session, skip: int = 0, limit: int = 50) -> list[dict]:
    """列出所有租戶（含 plan / is_active），分頁。"""
    rows = db.execute(
        select(Tenant).offset(skip).limit(limit)
    ).scalars().all()
    return [
        {
            "id": t.id,
            "name": t.name,
            "plan": t.plan,
            "is_active": t.is_active,
        }
        for t in rows
    ]


def get_tenant_usage(db: Session, tenant_id: int) -> dict:
    """今日租戶用量 + per-key 明細。"""
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found")

    today = datetime.date.today()
    limit = PLAN_DAILY_LIMITS.get(tenant.plan, PLAN_DAILY_LIMITS["free"])

    # tenant-level 今日用量
    usage_row = db.execute(
        select(ApiUsage).where(
            ApiUsage.tenant_id == tenant_id,
            ApiUsage.period == today,
        )
    ).scalar_one_or_none()
    today_count = usage_row.count if usage_row else 0

    # per-key 明細：取今日所有 ApiKeyUsage
    key_usages = db.execute(
        select(ApiKeyUsage, ApiKey)
        .join(ApiKey, ApiKeyUsage.api_key_id == ApiKey.id)
        .where(
            ApiKeyUsage.tenant_id == tenant_id,
            ApiKeyUsage.period == today,
        )
    ).all()

    per_key = [
        {
            "api_key_id": row.ApiKeyUsage.api_key_id,
            "key_name": row.ApiKey.name,
            "count": row.ApiKeyUsage.count,
        }
        for row in key_usages
    ]

    return {
        "tenant_id": tenant_id,
        "tenant_name": tenant.name,
        "plan": tenant.plan,
        "period": today.isoformat(),
        "today_count": today_count,
        "limit": limit,
        "remaining": max(0, limit - today_count),
        "per_key": per_key,
    }


def patch_tenant(
    db: Session,
    tenant_id: int,
    is_active: bool | None,
    plan: str | None,
    actor_user_id: int,
) -> dict:
    """停/啟用租戶 or 改方案（兩者可同時）。

    改方案複用 billing service（含 FOR UPDATE + 歷程記錄）。
    停用/啟用直接寫 is_active，不產生 PlanChangeHistory。
    """
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found")

    # 先改方案（可能 raise 409/400，需在 is_active 寫入前失敗）
    if plan is not None and plan != tenant.plan:
        _validate_plan(plan)
        current_order = _PLAN_ORDER.get(tenant.plan, 0)
        new_order = _PLAN_ORDER.get(plan, 0)

        # import 在此避免循環 import（billing → models，admin → billing）
        from saas_mvp.services.billing import upgrade_plan, downgrade_plan

        if new_order >= current_order:
            upgrade_plan(db, tenant, plan, actor_user_id, reason="admin_patch")
        else:
            downgrade_plan(db, tenant, plan, actor_user_id)
        # billing 函式已 commit；refresh tenant 讓後續讀到最新 plan
        db.refresh(tenant)

    # 停用/啟用
    if is_active is not None:
        tenant.is_active = is_active
        db.commit()

    return {
        "id": tenant.id,
        "name": tenant.name,
        "plan": tenant.plan,
        "is_active": tenant.is_active,
    }


def list_api_keys(db: Session, skip: int = 0, limit: int = 50) -> list[dict]:
    """跨租戶 API key 概況（含 is_active）。"""
    rows = db.execute(
        select(ApiKey).offset(skip).limit(limit)
    ).scalars().all()
    return [
        {
            "id": k.id,
            "name": k.name,
            "tenant_id": k.tenant_id,
            "user_id": k.user_id,
            "key_prefix": k.key_prefix,
            "is_active": k.is_active,
            "created_at": k.created_at.isoformat() if k.created_at else None,
        }
        for k in rows
    ]


def _validate_plan(plan: str) -> None:
    if plan not in PLAN_DAILY_LIMITS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown plan: '{plan}'. Valid: {sorted(PLAN_DAILY_LIMITS)}",
        )
