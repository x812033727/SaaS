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

import dataclasses
import datetime
import logging
import secrets

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.models.plan_change_history import PlanChangeHistory
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.usage import ApiUsage
from saas_mvp.quota import PLAN_DAILY_LIMITS

_log = logging.getLogger(__name__)


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


# ── 進階功能訂閱 / 退訂（橫向 feature flags） ─────────────────────────────────

@dataclasses.dataclass
class SubscribeResult:
    """訂閱結果：stub 立即開通回 payment_id；ecpay 待付款回 checkout_url。"""

    mode: str  # "stub" | "ecpay"
    enabled: bool
    payment_id: str | None = None
    checkout_url: str | None = None


def subscribe_feature(
    db: Session, tenant: Tenant, feature: str, actor_user_id: int
) -> SubscribeResult:
    """訂閱進階功能。

    * stub（預設）：模擬付款 → 立即 set_enabled(True)，回 payment_id。
    * ecpay：建立 pending 定期定額訂閱、**尚未開通**，回綠界 checkout 頁網址；
      功能待首期授權回調成功才開通。
    """
    from saas_mvp.services import features as features_svc

    features_svc.validate_feature(feature)

    if settings.payment_provider == "ecpay":
        from saas_mvp.services import subscriptions as subs_svc

        sub = subs_svc.create_subscription(
            db,
            tenant_id=tenant.id,
            feature=feature,
            amount_cents=settings.feature_monthly_price_cents,
            exec_times=settings.ecpay_period_exec_times,
        )
        base = settings.public_base_url.rstrip("/")
        return SubscribeResult(
            mode="ecpay",
            enabled=False,
            checkout_url=f"{base}/payments/ecpay/subscribe/{sub.id}",
        )

    payment_id = _generate_payment_id()  # stub 付款
    features_svc.set_enabled(
        db, tenant.id, feature, True,
        actor_user_id=actor_user_id, source="subscribe", reason=payment_id,
    )
    return SubscribeResult(mode="stub", enabled=True, payment_id=payment_id)


def unsubscribe_feature(
    db: Session, tenant: Tenant, feature: str, actor_user_id: int
) -> None:
    """退訂進階功能：關閉 feature（含稽核）。

    ecpay 模式：先呼叫綠界 CreditCardPeriodAction **真的停掉後續扣款**，再關閉功能。
    停扣 API 失敗時仍關閉功能，但把訂閱標 cancel_failed + log，避免關功能卻持續扣卡
    （待 ops 重試），絕不靜默放任。
    """
    from saas_mvp.services import features as features_svc

    features_svc.validate_feature(feature)

    if settings.payment_provider == "ecpay":
        from saas_mvp.services import subscriptions as subs_svc
        from saas_mvp.services.payment_ecpay import EcpayClient

        sub = subs_svc.latest_active_for(db, tenant.id, feature)
        if sub is not None:
            ok = False
            try:
                resp = EcpayClient().cancel_period(sub.merchant_trade_no)
                ok = str(resp.get("RtnCode")) == "1"
            except Exception:  # noqa: BLE001 — 網路/解析失敗不得阻擋退訂
                _log.exception(
                    "ecpay cancel_period raised for trade_no=%s", sub.merchant_trade_no
                )
                ok = False
            subs_svc.mark_cancelled(db, sub, ok=ok)
            if not ok:
                _log.warning(
                    "unsubscribe %s: ECPay stop-charge NOT confirmed (trade_no=%s); "
                    "flagged cancel_failed for ops retry",
                    feature, sub.merchant_trade_no,
                )

    features_svc.set_enabled(
        db, tenant.id, feature, False,
        actor_user_id=actor_user_id, source="unsubscribe",
    )
