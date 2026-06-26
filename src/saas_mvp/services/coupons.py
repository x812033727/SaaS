"""優惠券服務 — CRUD（純 REST 拋 HTTPException）+ 原子核銷（拋自訂例外，供 webhook 共用）。

核銷原子性：SELECT … FOR UPDATE 鎖 coupon 列 → 鎖內重驗有效性與額度 → INSERT redemption
（unique 擋一人一券）→ redeemed_count += 1 → 單一 commit。比照 quota / booking 鎖法。
"""

from __future__ import annotations

import datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from saas_mvp.models.coupon import (
    DISCOUNT_AMOUNT,
    DISCOUNT_PERCENT,
    VALID_DISCOUNT_TYPES,
    Coupon,
)
from saas_mvp.models.coupon_redemption import CouponRedemption
from saas_mvp.services.tenants import tenant_query


# ── 核銷例外（router 轉 HTTP；webhook 轉友善訊息） ────────────────────────────
class CouponError(Exception):
    """優惠券核銷錯誤基底。"""


class CouponNotFound(CouponError):
    pass


class CouponInactive(CouponError):
    pass


class CouponExpired(CouponError):
    pass


class CouponExhausted(CouponError):
    pass


class AlreadyRedeemed(CouponError):
    pass


class MinSpendNotMet(CouponError):
    """訂單小計未達券的最低消費門檻。"""


def compute_discount(coupon: Coupon, subtotal_cents: int) -> int:
    """券對某訂單小計可折抵的金額（cents）。

    percent → 小計 * value/100（無條件捨去）；amount → min(value, 小計)；
    gift/upsell（贈品/加購）不折抵訂單金額，回 0。
    """
    if coupon.discount_type == DISCOUNT_PERCENT:
        return max(0, subtotal_cents) * coupon.discount_value // 100
    if coupon.discount_type == DISCOUNT_AMOUNT:
        return min(coupon.discount_value, max(0, subtotal_cents))
    return 0  # gift / upsell


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _naive(dt: datetime.datetime | None) -> datetime.datetime | None:
    """SQLite 讀回為 naive；比較前統一去除 tzinfo 避免 aware/naive 混比。"""
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


# ── 店家 CRUD（純 REST，拋 HTTPException） ────────────────────────────────────

def _get_or_404(db: Session, tenant_id: int, coupon_id: int) -> Coupon:
    coupon = (
        tenant_query(db, Coupon, tenant_id).filter(Coupon.id == coupon_id).first()
    )
    if coupon is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Coupon not found"
        )
    return coupon


def create_coupon(
    db: Session,
    *,
    tenant_id: int,
    code: str,
    name: str,
    discount_type: str,
    discount_value: int,
    max_redemptions: int | None = None,
    min_spend_cents: int = 0,
    active_from: datetime.datetime | None = None,
    active_until: datetime.datetime | None = None,
) -> Coupon:
    if discount_type not in VALID_DISCOUNT_TYPES:
        raise HTTPException(
            status_code=422, detail=f"Invalid discount_type: {discount_type!r}"
        )
    if discount_value < 0:
        raise HTTPException(status_code=422, detail="discount_value must be >= 0")
    if discount_type == DISCOUNT_PERCENT and discount_value > 100:
        raise HTTPException(status_code=422, detail="percent discount must be 0-100")
    if min_spend_cents < 0:
        raise HTTPException(status_code=422, detail="min_spend_cents must be >= 0")
    coupon = Coupon(
        tenant_id=tenant_id,
        code=code,
        name=name,
        discount_type=discount_type,
        discount_value=discount_value,
        min_spend_cents=min_spend_cents,
        max_redemptions=max_redemptions,
        active_from=active_from,
        active_until=active_until,
    )
    db.add(coupon)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A coupon with this code already exists",
        )
    db.refresh(coupon)
    return coupon


def list_coupons(db: Session, *, tenant_id: int) -> list[Coupon]:
    return tenant_query(db, Coupon, tenant_id).order_by(Coupon.id.desc()).all()


def get_coupon(db: Session, *, tenant_id: int, coupon_id: int) -> Coupon:
    return _get_or_404(db, tenant_id, coupon_id)


def update_coupon(
    db: Session,
    *,
    tenant_id: int,
    coupon_id: int,
    name: str | None = None,
    max_redemptions: int | None = None,
    active_until: datetime.datetime | None = None,
    is_active: bool | None = None,
) -> Coupon:
    coupon = _get_or_404(db, tenant_id, coupon_id)
    if name is not None:
        coupon.name = name
    if max_redemptions is not None:
        coupon.max_redemptions = max_redemptions
    if active_until is not None:
        coupon.active_until = active_until
    if is_active is not None:
        coupon.is_active = is_active
    db.commit()
    db.refresh(coupon)
    return coupon


def deactivate_coupon(db: Session, *, tenant_id: int, coupon_id: int) -> None:
    coupon = _get_or_404(db, tenant_id, coupon_id)
    coupon.is_active = False
    db.commit()


def delete_coupon(db: Session, *, tenant_id: int, coupon_id: int) -> None:
    """刪除優惠券；已有兌換紀錄者擋下（請改用停用，保留稽核軌跡）。"""
    coupon = _get_or_404(db, tenant_id, coupon_id)
    redeemed = (
        tenant_query(db, CouponRedemption, tenant_id)
        .filter(CouponRedemption.coupon_id == coupon_id)
        .first()
    )
    if redeemed is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="此券已有兌換紀錄，請改用停用",
        )
    db.delete(coupon)
    db.commit()


def list_redemptions(
    db: Session, *, tenant_id: int, coupon_id: int
) -> list[CouponRedemption]:
    _get_or_404(db, tenant_id, coupon_id)  # 確認券屬本租戶
    return (
        tenant_query(db, CouponRedemption, tenant_id)
        .filter(CouponRedemption.coupon_id == coupon_id)
        .order_by(CouponRedemption.id.desc())
        .all()
    )


# ── 原子核銷（拋自訂例外，REST 與 webhook 共用） ──────────────────────────────

def redeem_coupon_core(
    db: Session,
    *,
    tenant_id: int,
    code: str,
    line_user_id: str,
    customer_id: int | None = None,
    reservation_id: int | None = None,
    order_id: int | None = None,
    subtotal_cents: int | None = None,
) -> tuple[Coupon, CouponRedemption]:
    """核銷核心：鎖券列 → 重驗有效性/額度/最低消費 → flush redemption（不 commit）。

    不負責 commit，供 create_order 等多寫流程在同一交易內套券後一次 commit。
    傳入 ``subtotal_cents`` 時會檢查 min_spend_cents 門檻（預約核銷可省略）。
    一人一券由 unique(coupon_id, line_user_id) 於 flush 時觸發。
    """
    coupon = db.execute(
        select(Coupon)
        .where(Coupon.tenant_id == tenant_id, Coupon.code == code)
        .with_for_update()
    ).scalar_one_or_none()
    if coupon is None:
        raise CouponNotFound(f"coupon {code!r} not found")
    if not coupon.is_active:
        raise CouponInactive(f"coupon {code!r} is inactive")

    now = _utcnow().replace(tzinfo=None)
    af = _naive(coupon.active_from)
    au = _naive(coupon.active_until)
    if (af is not None and now < af) or (au is not None and now > au):
        raise CouponExpired(f"coupon {code!r} not in active window")

    if (
        coupon.max_redemptions is not None
        and (coupon.redeemed_count or 0) >= coupon.max_redemptions
    ):
        raise CouponExhausted(f"coupon {code!r} fully redeemed")

    min_spend = coupon.min_spend_cents or 0
    if subtotal_cents is not None and subtotal_cents < min_spend:
        raise MinSpendNotMet(
            f"coupon {code!r} requires min spend {min_spend} (got {subtotal_cents})"
        )

    redemption = CouponRedemption(
        tenant_id=tenant_id,
        coupon_id=coupon.id,
        customer_id=customer_id,
        line_user_id=line_user_id,
        reservation_id=reservation_id,
        order_id=order_id,
    )
    db.add(redemption)
    try:
        db.flush()  # 觸發 unique(coupon_id, line_user_id)
    except IntegrityError:
        db.rollback()
        raise AlreadyRedeemed(f"coupon {code!r} already redeemed by this user")

    coupon.redeemed_count = (coupon.redeemed_count or 0) + 1
    return coupon, redemption


def redeem_coupon(
    db: Session,
    *,
    tenant_id: int,
    code: str,
    line_user_id: str,
    customer_id: int | None = None,
    reservation_id: int | None = None,
) -> CouponRedemption:
    """核銷一張券（獨立交易；REST 與 webhook 預約核銷用）。"""
    _, redemption = redeem_coupon_core(
        db,
        tenant_id=tenant_id,
        code=code,
        line_user_id=line_user_id,
        customer_id=customer_id,
        reservation_id=reservation_id,
    )
    db.commit()
    db.refresh(redemption)
    return redemption
