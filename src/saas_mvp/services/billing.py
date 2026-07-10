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


# ── 方案 bundle 訂閱（B2）：複用 FeatureSubscription 機制，偽 feature key ──────

def _ecpay_cancel_active_bundle_subs(db: Session, tenant_id: int) -> None:
    """停掉該租戶所有生效中的 bundle 訂閱扣款（升降級換約/退訂共用）。

    停扣失敗比照 unsubscribe_feature：標 cancel_failed 留給 ops 重試，不阻擋流程。
    """
    from saas_mvp.services import features as features_svc
    from saas_mvp.services import subscriptions as subs_svc
    from saas_mvp.services.payment_ecpay import EcpayClient

    for bundle_key in features_svc.VALID_BUNDLES:
        sub = subs_svc.latest_active_for(db, tenant_id, bundle_key)
        if sub is None:
            continue
        ok = False
        try:
            resp = EcpayClient().cancel_period(sub.merchant_trade_no)
            ok = str(resp.get("RtnCode")) == "1"
        except Exception:  # noqa: BLE001 — 網路失敗不得阻擋換約/退訂
            _log.exception(
                "ecpay cancel_period raised for bundle trade_no=%s",
                sub.merchant_trade_no,
            )
        subs_svc.mark_cancelled(db, sub, ok=ok)
        if not ok:
            _log.warning(
                "bundle unsubscribe: ECPay stop-charge NOT confirmed (trade_no=%s); "
                "flagged cancel_failed for ops retry",
                sub.merchant_trade_no,
            )


def subscribe_bundle(
    db: Session, tenant: Tenant, bundle_key: str, actor_user_id: int
) -> SubscribeResult:
    """訂閱方案 bundle（BUNDLE_STANDARD / BUNDLE_PRO）。

    * stub（預設）：模擬付款 → 立即改 tenant.plan + PlanChangeHistory + 清試用。
    * ecpay：先停掉既有 bundle 訂閱（換約＝立即生效、按月不找零，見 docs），
      再建 pending 訂閱導向綠界；plan 待首期授權回調成功才生效
      （apply_bundle_activation）。
    """
    from saas_mvp.services import features as features_svc
    from saas_mvp.services import plans as plans_svc

    if bundle_key not in features_svc.VALID_BUNDLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown bundle: {bundle_key!r}",
        )
    target_plan = features_svc.BUNDLE_TO_PLAN[bundle_key]
    if plans_svc.normalize_plan(tenant.plan) == target_plan:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Already on plan {target_plan!r}",
        )

    # C3 降級期末生效:pro→standard 時舊方案以 trial 機制保留至最後扣款+31 天
    # (與退訂寬限同機制同 anchor)。取捨:降級當下即開始扣新月費、舊方案功能
    # 保留至期末 —「多給不多收」,換零排程零新表;_apply_plan_change 的清 trial
    # 規則(新方案等級 ≥ trial 等級才清)保證首期回調不誤清。
    current_paid = plans_svc.normalize_plan(tenant.plan)
    if (
        current_paid != plans_svc.PLAN_FREE
        and plans_svc.PLAN_RANK[target_plan] < plans_svc.PLAN_RANK[current_paid]
    ):
        anchor = _bundle_grace_anchor(db, tenant.id)
        tenant.trial_plan = current_paid
        tenant.trial_ends_at = anchor + datetime.timedelta(days=31)
        db.commit()

    if settings.payment_provider == "ecpay":
        from saas_mvp.services import subscriptions as subs_svc

        _ecpay_cancel_active_bundle_subs(db, tenant.id)
        sub = subs_svc.create_subscription(
            db,
            tenant_id=tenant.id,
            feature=bundle_key,
            amount_cents=plans_svc.plan_price_cents(target_plan),
            exec_times=settings.ecpay_period_exec_times,
        )
        base = settings.public_base_url.rstrip("/")
        return SubscribeResult(
            mode="ecpay",
            enabled=False,
            checkout_url=f"{base}/payments/ecpay/subscribe/{sub.id}",
        )

    # stub：立即生效（模擬付款）
    payment_id = _generate_payment_id()
    _apply_plan_change(db, tenant, target_plan, actor_user_id, reason=payment_id)
    return SubscribeResult(mode="stub", enabled=True, payment_id=payment_id)


def unsubscribe_bundle(db: Session, tenant: Tenant, actor_user_id: int) -> None:
    """退訂方案 bundle → 降回 free。

    寬限：已付費者保留原方案至「最後扣款日 + 31 天」（複用 trial 機制，
    effective_plan 到期即刻降回），不立即沒收當期已付的功能。
    """
    from saas_mvp.services import features as features_svc
    from saas_mvp.services import plans as plans_svc
    from saas_mvp.services import subscriptions as subs_svc

    old_plan = plans_svc.normalize_plan(tenant.plan)

    if settings.payment_provider == "ecpay":
        _ecpay_cancel_active_bundle_subs(db, tenant.id)

    if old_plan != "free":
        anchor = _bundle_grace_anchor(db, tenant.id)
        tenant.trial_plan = old_plan
        tenant.trial_ends_at = anchor + datetime.timedelta(days=31)
    _apply_plan_change(db, tenant, "free", actor_user_id, reason="bundle_unsubscribe")


def _bundle_grace_anchor(db: Session, tenant_id: int) -> datetime.datetime:
    """寬限起算點:最後一次 bundle 扣款日;無扣款紀錄(stub)以現在起算。

    降級(C3)與退訂共用 — 同機制、同 anchor、同 31 天。
    """
    from saas_mvp.services import features as features_svc
    from saas_mvp.services import subscriptions as subs_svc

    anchor = None
    for bundle_key in features_svc.VALID_BUNDLES:
        sub = subs_svc.latest_active_for(db, tenant_id, bundle_key)
        if sub is not None and sub.last_charged_at is not None:
            anchor = sub.last_charged_at
    if anchor is None:
        anchor = datetime.datetime.now(datetime.timezone.utc)
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=datetime.timezone.utc)
    return anchor


def _apply_plan_change(
    db: Session,
    tenant: Tenant,
    new_plan: str,
    actor_user_id: int | None,
    reason: str | None,
) -> None:
    """改 plan + 寫 PlanChangeHistory + commit（bundle 路徑共用）。

    trial 清除規則(C3):只在「新方案等級 ≥ trial_plan 等級」時清(轉正/
    同級重訂);降級與退訂的寬限(trial_plan=舊方案)因此不會被誤清 —
    新訂的 standard 首期回調 activate 時,寬限中的 pro 试用欄位保留。
    """
    from saas_mvp.services.plans import PLAN_RANK, normalize_plan

    from_plan = tenant.plan
    tenant.plan = new_plan
    if tenant.trial_plan and PLAN_RANK[normalize_plan(new_plan)] >= PLAN_RANK[
        normalize_plan(tenant.trial_plan)
    ]:
        tenant.trial_plan = None
        tenant.trial_ends_at = None
    _insert_history(db, tenant, from_plan, new_plan, actor_user_id, reason=reason)
    db.commit()


def apply_bundle_activation(db: Session, sub) -> None:
    """ecpay 首期授權成功回調：套用 bundle → tenant.plan（含歷程、清試用）。"""
    from saas_mvp.services import features as features_svc

    target_plan = features_svc.BUNDLE_TO_PLAN.get(sub.feature)
    if target_plan is None:  # pragma: no cover - 呼叫端已過濾
        return
    tenant = db.get(Tenant, sub.tenant_id)
    if tenant is None:  # pragma: no cover - 防禦
        _log.warning("bundle activation: tenant %d not found", sub.tenant_id)
        return
    _apply_plan_change(
        db, tenant, target_plan, None, reason=f"bundle:{sub.merchant_trade_no}"
    )


def apply_bundle_period(db: Session, sub, *, success: bool) -> None:
    """ecpay 每期授權回調：成功維持方案；失敗降 free（扣款失敗=最大流失點，必留紀錄）。"""
    from saas_mvp.services import features as features_svc

    target_plan = features_svc.BUNDLE_TO_PLAN.get(sub.feature)
    if target_plan is None:  # pragma: no cover
        return
    tenant = db.get(Tenant, sub.tenant_id)
    if tenant is None:  # pragma: no cover
        return
    if success:
        if tenant.plan != target_plan:
            _apply_plan_change(
                db, tenant, target_plan, None,
                reason=f"bundle_period:{sub.merchant_trade_no}",
            )
        return
    _log.warning(
        "bundle period charge FAILED for tenant %d (trade_no=%s); downgrading to free",
        sub.tenant_id, sub.merchant_trade_no,
    )
    _apply_plan_change(
        db, tenant, "free", None,
        reason=f"bundle_charge_failed:{sub.merchant_trade_no}",
    )
    # 期扣失敗通知店家（C1）：扣款失敗是最大流失點。best-effort — 寄信失敗
    # 絕不影響回調結果（綠界收不到 1|OK 會重放，反覆觸發降級路徑）。
    notify_charge_failed(
        db,
        tenant,
        plan_label=features_svc.BUNDLE_LABELS.get(sub.feature, sub.feature),
        period_no=(sub.total_success_times or 0) + 1,
    )


def notify_charge_failed(
    db: Session,
    tenant: Tenant,
    *,
    plan_label: str,
    period_no: int,
    mailer=None,
) -> None:
    """期扣失敗 email 通知該租戶所有 owner（C1）。永不拋錯（best-effort）。"""
    from saas_mvp.models.user import User
    from saas_mvp.services.mailer import get_mailer

    try:
        owners = db.execute(
            select(User).where(
                User.tenant_id == tenant.id,
                User.role == "owner",
            )
        ).scalars().all()
        if not owners:
            return
        effective_mailer = mailer or get_mailer()
        base = settings.public_base_url.rstrip("/") or ""
        body = (
            f"您好！\n\n「{tenant.name}」的「{plan_label}」第 {period_no} 期"
            "信用卡扣款失敗，帳號已轉為免費版（資料完整保留）。\n\n"
            "常見原因：卡片過期、額度不足或銀行拒絕。更新卡片後重新訂閱即可"
            f"無縫恢復：{base}/ui/plan\n"
        )
        for owner in owners:
            try:
                effective_mailer.send(
                    to=owner.email,
                    subject=f"「{plan_label}」扣款失敗，已轉為免費版 — LINE 預約平台",
                    body=body,
                )
            except Exception:  # noqa: BLE001 — 單封失敗不影響其他收件人
                _log.warning(
                    "charge-failed email send failed tenant=%d to=%s",
                    tenant.id, owner.email, exc_info=True,
                )
    except Exception:  # noqa: BLE001 — 通知永不影響金流回調
        _log.warning(
            "notify_charge_failed unexpected failure tenant=%d",
            tenant.id, exc_info=True,
        )
