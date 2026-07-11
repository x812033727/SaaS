"""進階功能定期定額訂閱服務層（綠界信用卡定期定額）。

僅在 ecpay 模式使用。狀態機：
  pending --(首期授權成功)--> active --(退訂且停扣成功)--> cancelled
        \\--(授權失敗)--> failed        \\--(退訂但停扣 API 失敗)--> cancel_failed

功能開通（features.set_enabled）由 router 回調驅動，本層只管 FeatureSubscription 列。
"""

from __future__ import annotations

import datetime
import secrets

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from saas_mvp.models.feature_subscription import (
    SUB_ACTIVE,
    SUB_CANCEL_FAILED,
    SUB_CANCELLED,
    SUB_FAILED,
    SUB_PENDING,
    FeatureSubscription,
)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _base36(n: int) -> str:
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    if n == 0:
        return "0"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = chars[r] + out
    return out


def _gen_trade_no() -> str:
    """≤20 字、英數的唯一 MerchantTradeNo（綠界要求）。"""
    return ("SB" + _base36(int(_utcnow().timestamp())) + secrets.token_hex(3))[:20]


def create_subscription(
    db: Session,
    *,
    tenant_id: int,
    feature: str,
    amount_cents: int,
    exec_times: int = 99,
    period_type: str = "M",
    frequency: int = 1,
) -> FeatureSubscription:
    """建立 pending 訂閱（產唯一 merchant_trade_no）；尚未開通功能。"""
    last_exc: Exception | None = None
    for _ in range(5):  # 唯一碼碰撞極罕見，仍重試數次
        sub = FeatureSubscription(
            tenant_id=tenant_id,
            feature=feature,
            merchant_trade_no=_gen_trade_no(),
            status=SUB_PENDING,
            period_amount_cents=amount_cents,
            period_type=period_type,
            frequency=frequency,
            exec_times=exec_times,
        )
        db.add(sub)
        try:
            db.commit()
        except IntegrityError as exc:  # merchant_trade_no 撞號 → 重產
            db.rollback()
            last_exc = exc
            continue
        db.refresh(sub)
        return sub
    raise RuntimeError("could not allocate unique merchant_trade_no") from last_exc


def get_subscription_by_id(db: Session, subscription_id: int) -> FeatureSubscription | None:
    """依主鍵查訂閱（checkout 頁用）。"""
    return db.get(FeatureSubscription, subscription_id)


def get_subscription_by_trade_no(
    db: Session, merchant_trade_no: str
) -> FeatureSubscription | None:
    """依綠界唯一交易編號查訂閱（回調用，不分租戶）。"""
    return db.execute(
        select(FeatureSubscription).where(
            FeatureSubscription.merchant_trade_no == merchant_trade_no
        )
    ).scalar_one_or_none()


def latest_active_for(
    db: Session, tenant_id: int, feature: str
) -> FeatureSubscription | None:
    """退訂時找該租戶該功能「生效中（pending/active）」最新一筆訂閱以停扣。"""
    return db.execute(
        select(FeatureSubscription)
        .where(
            FeatureSubscription.tenant_id == tenant_id,
            FeatureSubscription.feature == feature,
            FeatureSubscription.status.in_((SUB_PENDING, SUB_ACTIVE)),
        )
        .order_by(FeatureSubscription.id.desc())
    ).scalars().first()


def _append_charge(
    db: Session,
    sub: FeatureSubscription,
    *,
    period_no: int,
    success: bool,
    gwsr: str | None = None,
    rtn_msg: str | None = None,
) -> None:
    """同交易 append 一筆逐期扣款明細（不 commit，由呼叫端一併提交）。

    以 (subscription_id, period_no, success) 查重：金流回調重放（綠界重送
    直到收到 1|OK）不會產生重複列。
    """
    from saas_mvp.models.subscription_charge import SubscriptionCharge

    existing = db.execute(
        select(SubscriptionCharge.id).where(
            SubscriptionCharge.subscription_id == sub.id,
            SubscriptionCharge.period_no == period_no,
            SubscriptionCharge.success == success,
        )
    ).first()
    if existing is not None:
        return
    db.add(SubscriptionCharge(
        tenant_id=sub.tenant_id,
        subscription_id=sub.id,
        period_no=period_no,
        success=success,
        amount_cents=sub.period_amount_cents,
        gwsr=gwsr,
        rtn_msg=(rtn_msg or "")[:255] or None,
    ))


def activate(
    db: Session,
    sub: FeatureSubscription,
    *,
    gwsr: str | None = None,
    auth_code: str | None = None,
) -> FeatureSubscription:
    """首期授權成功：標 active、記授權資訊、累計成功次數。"""
    if sub.status == SUB_ACTIVE:
        # 綠界首期授權回調「至少一次」投遞:已啟用即冪等返回。否則每次重送都會
        # 再累計 total_success_times 並 _append_charge 一筆新 period_no 的幻影扣款
        # 列(查重鍵含 period_no 故擋不住)→ 幻影電子發票 + 灌水成功期數。
        return sub
    sub.status = SUB_ACTIVE
    sub.activated_at = _utcnow()
    sub.last_charged_at = _utcnow()
    sub.total_success_times = (sub.total_success_times or 0) + 1
    if gwsr:
        sub.gwsr = gwsr
    if auth_code:
        sub.auth_code = auth_code
    _append_charge(
        db, sub, period_no=sub.total_success_times, success=True, gwsr=gwsr
    )
    db.commit()
    db.refresh(sub)
    return sub


def record_period(
    db: Session,
    sub: FeatureSubscription,
    *,
    success: bool,
    total_success_times: int | None = None,
) -> FeatureSubscription:
    """每期授權回調：成功累計、維持 active；失敗標 failed。"""
    if success:
        sub.last_charged_at = _utcnow()
        if total_success_times is not None:
            sub.total_success_times = total_success_times
        else:
            sub.total_success_times = (sub.total_success_times or 0) + 1
        if sub.status == SUB_PENDING:
            sub.status = SUB_ACTIVE
            sub.activated_at = sub.activated_at or _utcnow()
        _append_charge(
            db, sub, period_no=sub.total_success_times, success=True
        )
    else:
        sub.status = SUB_FAILED
        # 失敗期記「嘗試的期數」= 目前成功數 + 1
        _append_charge(
            db,
            sub,
            period_no=(sub.total_success_times or 0) + 1,
            success=False,
            rtn_msg="period charge failed",
        )
    db.commit()
    db.refresh(sub)
    return sub


def mark_failed(db: Session, sub: FeatureSubscription) -> FeatureSubscription:
    """首期授權失敗。"""
    sub.status = SUB_FAILED
    _append_charge(
        db, sub, period_no=1, success=False, rtn_msg="first auth failed"
    )
    db.commit()
    db.refresh(sub)
    return sub


def mark_cancelled(
    db: Session, sub: FeatureSubscription, *, ok: bool
) -> FeatureSubscription:
    """退訂：ok=True 綠界停扣成功 → cancelled；False → cancel_failed（待 ops 重試）。"""
    sub.status = SUB_CANCELLED if ok else SUB_CANCEL_FAILED
    sub.cancelled_at = _utcnow()
    db.commit()
    db.refresh(sub)
    return sub
