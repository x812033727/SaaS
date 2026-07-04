"""會員集點/等級服務。

點數彙總在 Customer.points_balance，每筆異動寫 PointTransaction（append-only）。
等級由 points_balance 對照 TIER_THRESHOLDS 即時重算（純函式，易測）。

earn_points / redeem_points **不 commit**——由呼叫端（如 booking.book_slot）在同一交易
統一 commit，確保「建單 ⇔ 集點」原子一致。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from saas_mvp.models.customer import Customer
from saas_mvp.models.point_transaction import PointTransaction

# (門檻點數, 等級)；由高到低，recompute_tier 取第一個 <= balance 者。
TIER_THRESHOLDS: list[tuple[int, str]] = [
    (500, "gold"),
    (100, "silver"),
    (0, "regular"),
]


class MembershipError(Exception):
    """會員點數相關錯誤基底。"""


class InsufficientPoints(MembershipError):
    """扣點時餘額不足。"""


def recompute_tier(points_balance: int) -> str:
    """依點數餘額算等級（純函式）。"""
    for threshold, tier in TIER_THRESHOLDS:
        if points_balance >= threshold:
            return tier
    return "regular"


def tier_discount_percent(tier: str | None) -> int:
    """會員等級對應的結帳折扣百分比（對標 vibeaico「不同等級不同折扣」）。

    取自 settings（可由環境覆寫）；未知等級回 0。
    """
    from saas_mvp.config import settings

    return {
        "gold": settings.tier_discount_gold_percent,
        "silver": settings.tier_discount_silver_percent,
        "regular": settings.tier_discount_regular_percent,
    }.get(tier or "regular", 0)


def tier_discount_for(tier: str | None, subtotal_cents: int) -> int:
    """等級折扣金額（cents），不超過小計。"""
    pct = tier_discount_percent(tier)
    if pct <= 0 or subtotal_cents <= 0:
        return 0
    return min(subtotal_cents * pct // 100, subtotal_cents)


def earn_points(
    db: Session,
    *,
    tenant_id: int,
    customer: Customer,
    delta: int,
    reason: str,
    reservation_id: int | None = None,
) -> PointTransaction | None:
    """集點：balance += delta、寫帳本、重算 tier（不 commit）。

    delta <= 0 直接 no-op 回 None（避免無意義帳本列）。
    """
    if delta <= 0:
        return None
    customer.points_balance = (customer.points_balance or 0) + delta
    customer.tier = recompute_tier(customer.points_balance)
    tx = PointTransaction(
        tenant_id=tenant_id,
        customer_id=customer.id,
        delta=delta,
        reason=reason,
        reservation_id=reservation_id,
    )
    db.add(tx)
    # PHASE 4-1：消費/集點達門檻觸發 'spend' 行銷（僅入列 pending，behind MARKETING_AUTO）。
    # reason 以 'campaign:' 開頭者為行銷派點本身，跳過以免遞迴觸發。
    if not reason.startswith("campaign:"):
        _maybe_enqueue_spend(db, tenant_id, customer)
    return tx


def _maybe_enqueue_spend(db: Session, tenant_id: int, customer: Customer) -> None:
    """集點達門檻時，若租戶開通 MARKETING_AUTO 則入列 spend CampaignSend（不 commit）。

    門檻語意：customer.points_balance >= spend 活動的 reward_value 視為達標
    （reward_value 在此既當門檻、也當點數獎勵）。最小化 hook、任何失敗吞掉。
    """
    try:
        from saas_mvp.services import features as features_svc

        if not features_svc.is_enabled(db, tenant_id, features_svc.MARKETING_AUTO):
            return
        from saas_mvp.services import marketing as marketing_svc

        marketing_svc.maybe_enqueue_spend_for_customer(db, tenant_id, customer)
    except Exception:  # noqa: BLE001 - spend hook must never break earn_points
        pass


def redeem_points(
    db: Session,
    *,
    tenant_id: int,
    customer: Customer,
    amount: int,
    reason: str,
    reservation_id: int | None = None,
) -> PointTransaction:
    """扣點：餘額不足拋 InsufficientPoints（不 commit）。"""
    if amount <= 0:
        raise ValueError(f"amount must be > 0, got {amount}")
    balance = customer.points_balance or 0
    if balance < amount:
        raise InsufficientPoints(f"insufficient points: have {balance}, need {amount}")
    customer.points_balance = balance - amount
    customer.tier = recompute_tier(customer.points_balance)
    tx = PointTransaction(
        tenant_id=tenant_id,
        customer_id=customer.id,
        delta=-amount,
        reason=reason,
        reservation_id=reservation_id,
    )
    db.add(tx)
    return tx


def get_membership(customer: Customer) -> dict:
    return {
        "points_balance": customer.points_balance or 0,
        "tier": customer.tier or "regular",
    }
