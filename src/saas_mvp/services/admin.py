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
from sqlalchemy.orm import Session, selectinload

from saas_mvp.models.api_key import ApiKey
from saas_mvp.models.api_key_usage import ApiKeyUsage
from saas_mvp.models.tenant import Tenant, normalize_store_type
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


def list_line_bots(
    db: Session,
    *,
    skip: int = 0,
    limit: int = 50,
    store_type: str | None = None,
    is_active: bool | None = None,
    uncategorized: bool = False,
) -> list[dict]:
    """跨店家 LINE bot 總覽（遮罩憑證，永不輸出明文 secret/token）。

    每列彙整：租戶資訊 + 是否已設定 LINE + 憑證狀態 + 今日用量。
    篩選（store_type / is_active / uncategorized）在 offset/limit 之前套用。
    LINE config 以 selectinload 一次撈齊、今日用量以單一 IN 查詢取得，
    兩者皆避免 per-tenant N+1。
    """
    stmt = select(Tenant).options(selectinload(Tenant.line_channel_config))
    if uncategorized:
        stmt = stmt.where(Tenant.store_type.is_(None))
    elif store_type is not None:
        stmt = stmt.where(Tenant.store_type == store_type)
    if is_active is not None:
        stmt = stmt.where(Tenant.is_active == is_active)

    tenants = db.execute(stmt.offset(skip).limit(limit)).scalars().all()
    if not tenants:
        return []

    # 今日用量：單一批次查（避免 N+1），用 dict 以 tenant_id 索引。
    today = datetime.date.today()
    tenant_ids = [t.id for t in tenants]
    usage_rows = db.execute(
        select(ApiUsage).where(
            ApiUsage.tenant_id.in_(tenant_ids),
            ApiUsage.period == today,
        )
    ).scalars().all()
    usage_by_tenant = {u.tenant_id: u for u in usage_rows}

    result: list[dict] = []
    for t in tenants:
        cfg = t.line_channel_config  # 一對一 relationship；未設定時為 None
        usage = usage_by_tenant.get(t.id)
        result.append(
            {
                "tenant_id": t.id,
                "name": t.name,
                "store_type": t.store_type,
                "plan": t.plan,
                "is_active": t.is_active,
                "has_line_config": cfg is not None,
                "has_channel_secret": bool(cfg.channel_secret_enc) if cfg else False,
                "has_access_token": bool(cfg.access_token_enc) if cfg else False,
                # 無 config 時回 None，以區分「尚未設定」與「設定但未驗證(unchecked)」。
                "credential_status": (
                    (cfg.credential_status or "unchecked") if cfg else None
                ),
                "line_bot_user_id": cfg.line_bot_user_id if cfg else None,
                "default_target_lang": cfg.default_target_lang if cfg else None,
                "today_count": usage.count if usage else 0,
                "today_chars": (usage.char_count or 0) if usage else 0,
            }
        )
    return result


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
    store_type: str | None = None,
    store_type_provided: bool = False,
) -> dict:
    """停/啟用租戶 or 改方案 or 設定店家類型（可同時）。

    改方案複用 billing service（含 FOR UPDATE + 歷程記錄）。
    停用/啟用直接寫 is_active，不產生 PlanChangeHistory。
    store_type 用 ``store_type_provided`` 旗標區分「未提供＝不動」與
    「提供 null＝清空為未分類」。
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

    # 停用/啟用 + 店家類型（同一交易寫入）
    dirty = False
    if is_active is not None:
        tenant.is_active = is_active
        dirty = True
    if store_type_provided:
        tenant.store_type = normalize_store_type(store_type)
        dirty = True
    if dirty:
        db.commit()
        db.refresh(tenant)

    return {
        "id": tenant.id,
        "name": tenant.name,
        "plan": tenant.plan,
        "is_active": tenant.is_active,
        "store_type": tenant.store_type,
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
