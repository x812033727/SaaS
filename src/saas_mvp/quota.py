"""Plan / quota 定義與計量邏輯。

配額規則
--------
* free : 每日 100 次 API 呼叫
* pro  : 每日 10 000 次 API 呼叫

超量時拋出 HTTP 429 並附說明訊息。
"""

from __future__ import annotations

import datetime

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.auth.dependencies import Actor, get_current_actor
from saas_mvp.db import get_db
from saas_mvp.models.usage import ApiUsage
from saas_mvp.models.user import User

# SQLite 不支援 SKIP LOCKED，但 with_for_update() 在 SQLite 會升為
# connection-level exclusive lock，足以序列化同一程序內的並發寫入。
# PostgreSQL 則完整使用 SELECT … FOR UPDATE 行鎖。

# ── 配額常數 ─────────────────────────────────────────────────
PLAN_DAILY_LIMITS: dict[str, int] = {
    "free": 100,
    "pro": 10_000,
}

_429 = HTTPException(
    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
    detail="Quota exceeded for today. Upgrade to pro for higher limits.",
)


def validate_count(value: object) -> int:
    """確認 value 是合法非負整數（排除 bool 混入）。"""
    if isinstance(value, bool):
        raise TypeError(f"Count must be int, got bool: {value!r}")
    if not isinstance(value, int):
        raise TypeError(f"Count must be int, got {type(value).__name__}: {value!r}")
    if value < 0:
        raise ValueError(f"Count must be >= 0, got {value}")
    return value


def _get_or_create_usage_locked(db: Session, tenant_id: int, today: datetime.date) -> ApiUsage:
    """取得今日計量列（帶 FOR UPDATE 鎖定），不存在則先 INSERT count=0。"""
    row = db.execute(
        select(ApiUsage)
        .where(
            ApiUsage.tenant_id == tenant_id,
            ApiUsage.period == today,
        )
        .with_for_update()
    ).scalar_one_or_none()

    if row is None:
        try:
            row = ApiUsage(tenant_id=tenant_id, period=today, count=0)
            db.add(row)
            db.flush()
        except Exception:
            db.rollback()
            row = db.execute(
                select(ApiUsage)
                .where(
                    ApiUsage.tenant_id == tenant_id,
                    ApiUsage.period == today,
                )
                .with_for_update()
            ).scalar_one()

    return row


def check_and_increment(db: Session, tenant_id: int, plan: str) -> int:
    """檢查今日配額；未超量則 +1 並 commit；超量拋 HTTP 429。

    使用 SELECT FOR UPDATE 序列化並發存取，消除 read-check-write 競態。
    回傳更新後的 count。
    """
    limit = PLAN_DAILY_LIMITS.get(plan, PLAN_DAILY_LIMITS["free"])
    today = datetime.date.today()

    row = _get_or_create_usage_locked(db, tenant_id, today)

    validate_count(row.count)   # 防衛性：DB 異常值（含 bool）一律攔截

    if row.count >= limit:
        raise _429

    row.count += 1
    db.commit()
    return row.count


def _get_or_create_key_usage_locked(
    db: Session, api_key_id: int, tenant_id: int, today: datetime.date
):
    """取得今日 per-key 計量列（帶 FOR UPDATE 鎖定）。"""
    from saas_mvp.models.api_key_usage import ApiKeyUsage  # 避免頂層循環 import

    row = db.execute(
        select(ApiKeyUsage)
        .where(
            ApiKeyUsage.api_key_id == api_key_id,
            ApiKeyUsage.period == today,
        )
        .with_for_update()
    ).scalar_one_or_none()

    if row is None:
        try:
            row = ApiKeyUsage(api_key_id=api_key_id, tenant_id=tenant_id,
                              period=today, count=0)
            db.add(row)
            db.flush()
        except Exception:
            db.rollback()
            row = db.execute(
                select(ApiKeyUsage)
                .where(
                    ApiKeyUsage.api_key_id == api_key_id,
                    ApiKeyUsage.period == today,
                )
                .with_for_update()
            ).scalar_one()

    return row


def check_and_increment_key(db: Session, api_key_id: int, tenant_id: int) -> int:
    """原子遞增 per-key 計量；DB 例外向上傳播（絕不靜默吞掉）。"""
    today = datetime.date.today()
    row = _get_or_create_key_usage_locked(db, api_key_id, tenant_id, today)
    row.count += 1
    db.commit()
    return row.count


def get_quota_status(db: Session, tenant_id: int, plan: str) -> dict:
    """回傳今日用量狀態（供 API 查詢）。"""
    today = datetime.date.today()
    limit = PLAN_DAILY_LIMITS.get(plan, PLAN_DAILY_LIMITS["free"])
    row = db.execute(
        select(ApiUsage).where(
            ApiUsage.tenant_id == tenant_id,
            ApiUsage.period == today,
        )
    ).scalar_one_or_none()
    used = row.count if row else 0
    return {
        "plan": plan,
        "period": today.isoformat(),
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used),
    }


# ── FastAPI dependency ────────────────────────────────────────

def require_quota(
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
) -> User:
    """dependency：驗證 tenant quota 並計量 +1；若以 API key 認證則同時累計 per-key 計數。

    用法（router 層）::

        @router.post("/notes", dependencies=[Depends(require_quota)])
        def create_note(...): ...
    """
    # 1. tenant-level 配額檢查（超量拋 429）
    check_and_increment(db, actor.user.tenant_id, actor.user.tenant.plan)
    # 2. per-key 計量（僅 API key 認證時，DB 例外一律傳播回 500）
    if actor.api_key_id is not None:
        check_and_increment_key(db, actor.api_key_id, actor.user.tenant_id)
    return actor.user
