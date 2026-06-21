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
    return tx


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
