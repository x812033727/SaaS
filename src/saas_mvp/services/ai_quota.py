"""AI 對話月度計量（A2.4）— 一比一比照 services/push_quota.py 的鎖定/後扣語意。

額度規則：
  allowance = ai_allowance_base + (ai_allowance_boost 若明確訂閱 AI_BOOST)
  超額行為：降級回引導式預約（不中斷服務），非硬擋。
"""

from __future__ import annotations

import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.models.ai_usage import AiUsage
from saas_mvp.models.tenant_feature import TenantFeature
from saas_mvp.services import features as features_svc


def _period_now(now: datetime.datetime | None = None) -> str:
    effective = now or datetime.datetime.now(datetime.timezone.utc)
    return effective.strftime("%Y%m")


def boost_enabled(db: Session, tenant_id: int) -> bool:
    """AI_BOOST 是付費加購：必須明確訂閱（無列=False），比照 PUSH_BOOST 語意。"""
    row = db.execute(
        select(TenantFeature).where(
            TenantFeature.tenant_id == tenant_id,
            TenantFeature.feature == features_svc.AI_BOOST,
        )
    ).scalar_one_or_none()
    return bool(row.enabled) if row is not None else False


def allowance(db: Session, tenant_id: int) -> int:
    base = settings.ai_allowance_base
    if boost_enabled(db, tenant_id):
        return base + settings.ai_allowance_boost
    return base


def get_usage(db: Session, tenant_id: int, period: str | None = None) -> int:
    effective_period = period or _period_now()
    row = db.execute(
        select(AiUsage).where(
            AiUsage.tenant_id == tenant_id,
            AiUsage.period == effective_period,
        )
    ).scalar_one_or_none()
    return (row.count or 0) if row else 0


def has_ai_quota(
    db: Session, tenant_id: int, *, now: datetime.datetime | None = None
) -> bool:
    """非遞增檢查（供「後扣」流程先放行）。"""
    period = _period_now(now)
    return get_usage(db, tenant_id, period) + 1 <= allowance(db, tenant_id)


def _get_or_create_locked(db: Session, tenant_id: int, period: str) -> AiUsage:
    row = db.execute(
        select(AiUsage)
        .where(AiUsage.tenant_id == tenant_id, AiUsage.period == period)
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        try:
            row = AiUsage(tenant_id=tenant_id, period=period, count=0)
            db.add(row)
            db.flush()
        except Exception:
            db.rollback()
            row = db.execute(
                select(AiUsage)
                .where(AiUsage.tenant_id == tenant_id, AiUsage.period == period)
                .with_for_update()
            ).scalar_one()
    return row


def consume_ai_in_txn(
    db: Session, tenant_id: int, *, now: datetime.datetime | None = None
) -> None:
    """count += 1（不 commit，由呼叫端與對話狀態一起提交）。後扣：AI 回覆成功才計。"""
    row = _get_or_create_locked(db, tenant_id, _period_now(now))
    row.count = (row.count or 0) + 1


def get_ai_quota_status(
    db: Session, tenant_id: int, *, now: datetime.datetime | None = None
) -> dict:
    """供 GET /quota/ai。"""
    period = _period_now(now)
    used = get_usage(db, tenant_id, period)
    total = allowance(db, tenant_id)
    return {
        "period": period,
        "used": used,
        "allowance": total,
        "remaining": max(0, total - used),
        "boost_enabled": boost_enabled(db, tenant_id),
    }
