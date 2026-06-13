"""Usage 路由 — 回傳租戶總量 + per-key 明細。

端點：
  GET /usage/   回傳 { tenant: {...}, api_keys: [...] }

回應欄位說明：
  tenant.plan          : 目前方案（free / pro）
  tenant.daily_limit   : 當日上限
  tenant.used_today    : 今日已用量（全部認證方式合計）
  tenant.remaining     : 剩餘量（max 0）
  tenant.period        : 計量日期（ISO 8601，UTC）
  api_keys[].api_key_id: API key ID
  api_keys[].name      : key 名稱
  api_keys[].key_prefix: key 隨機部分前 8 字元
  api_keys[].used_today: 今日透過該 key 的呼叫次數
  api_keys[].period    : 計量日期（ISO 8601，UTC）
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.auth.dependencies import get_current_user
from saas_mvp.db import get_db
from saas_mvp.models.api_key import ApiKey
from saas_mvp.models.api_key_usage import ApiKeyUsage
from saas_mvp.models.usage import ApiUsage
from saas_mvp.models.user import User
from saas_mvp.quota import PLAN_DAILY_LIMITS

router = APIRouter(prefix="/usage", tags=["usage"])


# ── Schemas ───────────────────────────────────────────────────

class TenantUsageSchema(BaseModel):
    plan: str
    daily_limit: int
    used_today: int
    remaining: int
    period: str   # ISO date string


class ApiKeyUsageItem(BaseModel):
    api_key_id: int
    name: str
    key_prefix: str
    used_today: int
    remaining: int   # max(0, tenant_daily_limit - used_today)；per-key 共享租戶配額
    period: str      # ISO date string


class UsageResponse(BaseModel):
    tenant: TenantUsageSchema
    api_keys: list[ApiKeyUsageItem]


# ── Endpoints ─────────────────────────────────────────────────

@router.get("/", response_model=UsageResponse)
def get_usage(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UsageResponse:
    """回傳目前租戶的今日用量總覽與各 API key 的明細。"""
    today = datetime.date.today()
    plan = current_user.tenant.plan
    daily_limit = PLAN_DAILY_LIMITS.get(plan, PLAN_DAILY_LIMITS["free"])

    # ── tenant-level 用量 ──────────────────────────────────────
    tenant_row = db.execute(
        select(ApiUsage).where(
            ApiUsage.tenant_id == current_user.tenant_id,
            ApiUsage.period == today,
        )
    ).scalar_one_or_none()
    used_today = tenant_row.count if tenant_row else 0

    # ── per-key 明細（join ApiKeyUsage + ApiKey）──────────────
    key_rows = db.execute(
        select(ApiKeyUsage, ApiKey)
        .join(ApiKey, ApiKeyUsage.api_key_id == ApiKey.id)
        .where(
            ApiKeyUsage.tenant_id == current_user.tenant_id,
            ApiKeyUsage.period == today,
        )
    ).all()

    api_key_items = [
        ApiKeyUsageItem(
            api_key_id=ku.api_key_id,
            name=k.name,
            key_prefix=k.key_prefix,
            used_today=ku.count,
            remaining=max(0, daily_limit - ku.count),
            period=today.isoformat(),
        )
        for ku, k in key_rows
    ]

    return UsageResponse(
        tenant=TenantUsageSchema(
            plan=plan,
            daily_limit=daily_limit,
            used_today=used_today,
            remaining=max(0, daily_limit - used_today),
            period=today.isoformat(),
        ),
        api_keys=api_key_items,
    )
