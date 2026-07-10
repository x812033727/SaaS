"""月度推播額度計量（vibeaico「Additional Push Notification Allowance」）。

每租戶每月（period 'YYYYMM'）有推播額度上限，跨所有 LINE push 路徑
（預約提醒、預約異動通知、行銷活動）共用同一計量器 models/push_usage.PushUsage。

額度規則：
  allowance = push_allowance_base + (push_allowance_boost 若開通 PUSH_BOOST 旗標)
  base 預設 200／月，PUSH_BOOST 加購 +500／月。

計量／鎖定模式**比照 quota.py**：
  * has_push_quota：非遞增檢查（不寫入、不 commit），供「後扣」流程先放行。
  * consume_push：SELECT … FOR UPDATE upsert 月度列、count += n、單一 commit
    （比照 quota._get_or_create_usage_locked），呼叫端應於推播**成功後**才扣
    （post-success debit，只計實際送出的推播；下游失敗不白扣）。
  * try_consume：has_push_quota 通過則 consume 並回 True，否則回 False；
    單 worker 下「先檢查再扣」之間的 TOCTOU 窗口可接受單次溢出（與 quota.py
    同語意：重點是「下次 has_push_quota 真的擋下」）。
"""

from __future__ import annotations

import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.models.push_usage import PushUsage
from saas_mvp.models.tenant_feature import TenantFeature
from saas_mvp.services import features as features_svc


def _period_now(now: datetime.datetime | None = None) -> str:
    """目前計量月份字串 'YYYYMM'（UTC）。

    接受可選的 now（供測試注入跨月情境，比照 reminders / ops 的 now 參數）。
    """
    effective = now or datetime.datetime.now(datetime.timezone.utc)
    return effective.strftime("%Y%m")


def boost_enabled(db: Session, tenant_id: int) -> bool:
    """該租戶是否已加購 PUSH_BOOST。

    刻意**不**走 features.is_enabled 的「無列回 features_default_enabled」語意：
    PUSH_BOOST 是付費加購額度，必須由租戶**明確訂閱**才生效（無 TenantFeature
    列＝未加購＝False），否則 features_default_enabled=True 的 dev/相容環境會讓
    所有租戶無償取得 +500 額度，違反計費意圖。
    """
    row = db.execute(
        select(TenantFeature).where(
            TenantFeature.tenant_id == tenant_id,
            TenantFeature.feature == features_svc.PUSH_BOOST,
        )
    ).scalar_one_or_none()
    return bool(row.enabled) if row is not None else False


def _plan_base_allowance(db: Session, tenant_id: int) -> int:
    """依 effective_plan（含試用）取方案基本推播額度。

    延遲 import plans 避免循環；tenant 查不到（防禦）回 free 基本值。
    """
    from saas_mvp.models.tenant import Tenant
    from saas_mvp.services import plans as plans_svc

    tenant = db.get(Tenant, tenant_id)
    if tenant is None:  # pragma: no cover - 防禦性
        return settings.push_allowance_base
    plan = plans_svc.effective_plan(tenant)
    if plan == plans_svc.PLAN_PRO:
        return settings.push_allowance_pro
    if plan == plans_svc.PLAN_STANDARD:
        return settings.push_allowance_standard
    return settings.push_allowance_base


def allowance(db: Session, tenant_id: int) -> int:
    """該租戶本月推播額度上限：方案基本額度（+ boost 若加購 PUSH_BOOST）。"""
    base = _plan_base_allowance(db, tenant_id)
    if boost_enabled(db, tenant_id):
        return base + settings.push_allowance_boost
    return base


def get_usage(
    db: Session, tenant_id: int, period: str | None = None
) -> int:
    """本月（或指定 period）已用推播則數；無計量列回 0。"""
    effective_period = period or _period_now()
    row = db.execute(
        select(PushUsage).where(
            PushUsage.tenant_id == tenant_id,
            PushUsage.period == effective_period,
        )
    ).scalar_one_or_none()
    return (row.count or 0) if row else 0


def has_push_quota(
    db: Session, tenant_id: int, *, now: datetime.datetime | None = None, n: int = 1
) -> bool:
    """非遞增檢查：本月再推 n 則是否仍在額度內（不寫入、不 commit）。

    回傳 True 代表仍有額度（current + n <= allowance），False 代表已達上限。
    """
    period = _period_now(now)
    used = get_usage(db, tenant_id, period)
    return used + n <= allowance(db, tenant_id)


def _get_or_create_push_usage_locked(
    db: Session, tenant_id: int, period: str
) -> PushUsage:
    """取得本月計量列（帶 FOR UPDATE 鎖定），不存在則先 INSERT count=0。

    比照 quota._get_or_create_usage_locked：SQLite 升為 connection-level
    exclusive lock 序列化同程序並發；PostgreSQL 走 row-level FOR UPDATE。
    """
    row = db.execute(
        select(PushUsage)
        .where(
            PushUsage.tenant_id == tenant_id,
            PushUsage.period == period,
        )
        .with_for_update()
    ).scalar_one_or_none()

    if row is None:
        try:
            row = PushUsage(tenant_id=tenant_id, period=period, count=0)
            db.add(row)
            db.flush()
        except Exception:
            db.rollback()
            row = db.execute(
                select(PushUsage)
                .where(
                    PushUsage.tenant_id == tenant_id,
                    PushUsage.period == period,
                )
                .with_for_update()
            ).scalar_one()

    return row


def consume_push_in_txn(
    db: Session, tenant_id: int, *, now: datetime.datetime | None = None, n: int = 1
) -> None:
    """同 consume_push 但**不 commit**，由呼叫端一併提交。

    供批次派送迴圈把「標 sent + 計量」合併為單一 commit（每筆 2 commits → 1），
    並保證兩者同交易原子落盤。
    """
    period = _period_now(now)
    row = _get_or_create_push_usage_locked(db, tenant_id, period)
    row.count = (row.count or 0) + n


def consume_push(
    db: Session, tenant_id: int, *, now: datetime.datetime | None = None, n: int = 1
) -> None:
    """原子遞增本月推播計量 count += n、單一 commit（不重驗額度）。

    語意採「後扣」：呼叫端應在推播**成功送出後**才呼叫，只計實際送出的推播。
    額度檢查由 has_push_quota / try_consume 於送出前負責。
    """
    consume_push_in_txn(db, tenant_id, now=now, n=n)
    db.commit()


def try_consume(
    db: Session, tenant_id: int, *, now: datetime.datetime | None = None
) -> bool:
    """便利包裝：若本月仍有額度則扣 1 並回 True，否則回 False（不扣）。

    在 FOR UPDATE 鎖內重驗額度後才遞增，單 worker 下足夠原子。
    """
    period = _period_now(now)
    row = _get_or_create_push_usage_locked(db, tenant_id, period)
    used = row.count or 0
    if used + 1 > allowance(db, tenant_id):
        db.rollback()
        return False
    row.count = used + 1
    db.commit()
    return True


def get_push_quota_status(
    db: Session, tenant_id: int, *, now: datetime.datetime | None = None
) -> dict:
    """本月推播額度狀態（供 GET /quota/push）。

    欄位：period / used / allowance / remaining / boost_enabled。
    """
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
