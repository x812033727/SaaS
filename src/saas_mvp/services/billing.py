"""帳單升降級服務層。

設計決策
--------
* upgrade_plan  — 升級或 checkout 通用；立即生效，同交易寫 PlanChangeHistory。
* downgrade_plan — 降級；先 SELECT … FOR UPDATE 讀今日用量，超量 raise HTTP 409；
                   否則同交易寫 plan + history。
* 兩函式回傳模擬 payment_id（"simulated_" + secrets.token_hex(6)），完全離線。
* actor.user.id 一律填入 changed_by_user_id；API key 認證時填 key 所屬 User.id。
"""

from __future__ import annotations

import datetime
import secrets

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.models.plan_change_history import PlanChangeHistory
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.usage import ApiUsage
from saas_mvp.quota import PLAN_DAILY_LIMITS


def _generate_payment_id() -> str:
    return "simulated_" + secrets.token_hex(6)


def _validate_plan(plan: str) -> None:
    if plan not in PLAN_DAILY_LIMITS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown plan: '{plan}'. Valid plans: {sorted(PLAN_DAILY_LIMITS)}",
        )


def _insert_history(
    db: Session,
    tenant: Tenant,
    from_plan: str,
    to_plan: str,
    actor_user_id: int,
    reason: str | None = None,
) -> None:
    db.add(
        PlanChangeHistory(
            tenant_id=tenant.id,
            from_plan=from_plan,
            to_plan=to_plan,
            changed_by_user_id=actor_user_id,
            changed_at=datetime.datetime.now(datetime.timezone.utc),
            reason=reason,
        )
    )


def upgrade_plan(
    db: Session,
    tenant: Tenant,
    new_plan: str,
    actor_user_id: int,
    reason: str | None = None,
) -> str:
    """立即生效：改 plan + 寫歷程，回傳模擬 payment_id。

    適用於升級（free→pro）或 checkout（任意方向），呼叫端自行確認方向合法性。
    """
    _validate_plan(new_plan)
    from_plan = tenant.plan
    tenant.plan = new_plan
    _insert_history(db, tenant, from_plan, new_plan, actor_user_id, reason=reason)
    db.commit()
    return _generate_payment_id()


def downgrade_plan(
    db: Session,
    tenant: Tenant,
    new_plan: str,
    actor_user_id: int,
) -> str:
    """降級 plan，以 SELECT … FOR UPDATE 防止 TOCTOU 競態。

    今日用量 > 新方案上限 → raise HTTP 409 with current_usage/new_limit。
    否則同交易寫 plan + history，回傳模擬 payment_id。
    """
    _validate_plan(new_plan)

    new_limit = PLAN_DAILY_LIMITS[new_plan]
    today = datetime.date.today()

    # 鎖住今日計量列，消除並發降級的 read-check-write 競態
    usage_row = db.execute(
        select(ApiUsage)
        .where(ApiUsage.tenant_id == tenant.id, ApiUsage.period == today)
        .with_for_update()
    ).scalar_one_or_none()

    current_usage = usage_row.count if usage_row else 0

    if current_usage > new_limit:
        # 無寫入，直接拋出（session 在 get_db finally 中 close）
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "current_usage_exceeds_new_limit",
                "current_usage": current_usage,
                "new_limit": new_limit,
            },
        )

    from_plan = tenant.plan
    tenant.plan = new_plan
    _insert_history(db, tenant, from_plan, new_plan, actor_user_id, reason="downgrade")
    db.commit()
    return _generate_payment_id()
