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

from saas_mvp.models.coupon import VALID_DISCOUNT_TYPES, Coupon
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
    active_from: datetime.datetime | None = None,
    active_until: datetime.datetime | None = None,
) -> Coupon:
    if discount_type not in VALID_DISCOUNT_TYPES:
        raise HTTPException(
            status_code=422, detail=f"Invalid discount_type: {discount_type!r}"
        )
    if discount_value < 0:
        raise HTTPException(status_code=422, detail="discount_value must be >= 0")
    if discount_type == "percent" and discount_value > 100:
        raise HTTPException(status_code=422, detail="percent discount must be 0-100")
    coupon = Coupon(
        tenant_id=tenant_id,
        code=code,
        name=name,
        discount_type=discount_type,
        discount_value=discount_value,
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

def redeem_coupon(
    db: Session,
    *,
    tenant_id: int,
    code: str,
    line_user_id: str,
    customer_id: int | None = None,
    reservation_id: int | None = None,
) -> CouponRedemption:
    """核銷一張券。鎖券列重驗有效性與額度後遞增；一人一券由 unique 約束擋。"""
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

    redemption = CouponRedemption(
        tenant_id=tenant_id,
        coupon_id=coupon.id,
        customer_id=customer_id,
        line_user_id=line_user_id,
        reservation_id=reservation_id,
    )
    db.add(redemption)
    try:
        db.flush()  # 觸發 unique(coupon_id, line_user_id)
    except IntegrityError:
        db.rollback()
        raise AlreadyRedeemed(f"coupon {code!r} already redeemed by this user")

    coupon.redeemed_count = (coupon.redeemed_count or 0) + 1
    db.commit()
    db.refresh(redemption)
    return redemption
