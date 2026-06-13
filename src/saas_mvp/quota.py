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

from saas_mvp.auth.dependencies import get_current_user
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
    """取得今日計量列（帶 FOR UPDATE 鎖定），不存在則先 INSERT count=0。

    FOR UPDATE 確保同一時刻只有一個 session 持有該列，消除
    「兩個 session 同時讀到 count=99 → 都通過檢查 → 都 +1」的競態。
    """
    row = db.execute(
        select(ApiUsage)
        .where(
            ApiUsage.tenant_id == tenant_id,
            ApiUsage.period == today,
        )
        .with_for_update()          # SELECT … FOR UPDATE（SQLite: connection-level lock）
    ).scalar_one_or_none()

    if row is None:
        # INSERT 前可能另一個 session 已先插入（SQLite: 不太可能，PG: 可能）
        # 用 get_or_create 模式：INSERT 若 UNIQUE 衝突則 SELECT 再鎖一次
        try:
            row = ApiUsage(tenant_id=tenant_id, period=today, count=0)
            db.add(row)
            db.flush()          # 取得 id；尚未 commit，可回滾
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
    today = datetime.datetime.now(datetime.timezone.utc).date()

    row = _get_or_create_usage_locked(db, tenant_id, today)

    validate_count(row.count)   # 防衛性：DB 異常值（含 bool）一律攔截

    if row.count >= limit:
        raise _429

    row.count += 1
    db.commit()
    return row.count


def get_quota_status(db: Session, tenant_id: int, plan: str) -> dict:
    """回傳今日用量狀態（供 API 查詢）。"""
    today = datetime.datetime.now(datetime.timezone.utc).date()
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
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    """dependency：驗證 quota 並計量 +1，超量回 429。

    用法（router 層）::

        @router.post("/notes", dependencies=[Depends(require_quota)])
        def create_note(...): ...
    """
    check_and_increment(db, current_user.tenant_id, current_user.tenant.plan)
    return current_user
