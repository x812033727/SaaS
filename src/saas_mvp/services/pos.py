"""POS 結帳服務（PHASE 4-1）— 電話查會員 + 原子結帳。

lookup_by_phone：以 tenant_id + phone 查顧客，回傳 {customer, points_balance,
active_coupons}；查無回 None（router 轉 404）。

checkout：單一交易內完成—
  - 建 Order + OrderItems（複用 shop 建單邏輯：鎖商品、驗庫存、扣庫存、快照單價）。
  - （給定 customer 時）鎖顧客列 FOR UPDATE 取得點數原子性（比照 book_slot 鎖法）。
  - 套用優惠券折扣（coupons.redeem_coupon）。
  - 折抵點數（membership.redeem_points；不足拋 InsufficientPoints）。
  - 對淨付金額回贈點數（membership.earn_points）。
  - 給定 reservation_id 時標該預約到場（mark_attendance 行內邏輯）。
  全程單一 commit；任一失敗整筆 rollback（壞券/庫存或點數不足 → 訂單不建立）。
"""

from __future__ import annotations

import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.models.coupon import Coupon
from saas_mvp.models.customer import Customer
from saas_mvp.models.order import ORDER_PENDING, Order
from saas_mvp.models.order_item import OrderItem
from saas_mvp.models.product import Product
from saas_mvp.models.reservation import Reservation
from saas_mvp.services import membership as membership_svc
from saas_mvp.services.tenants import tenant_query


class POSError(Exception):
    """POS 結帳錯誤基底。"""


class CustomerNotFound(POSError):
    pass


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _active_coupons(db: Session, tenant_id: int) -> list[Coupon]:
    now = _utcnow().replace(tzinfo=None)

    def _naive(dt):
        if dt is None:
            return None
        return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt

    out: list[Coupon] = []
    for c in tenant_query(db, Coupon, tenant_id).filter(Coupon.is_active.is_(True)):
        af = _naive(c.active_from)
        au = _naive(c.active_until)
        if af is not None and now < af:
            continue
        if au is not None and now > au:
            continue
        if (
            c.max_redemptions is not None
            and (c.redeemed_count or 0) >= c.max_redemptions
        ):
            continue
        out.append(c)
    return out


def lookup_by_phone(db: Session, *, tenant_id: int, phone: str) -> dict | None:
    """以電話查會員；回傳 {customer, points_balance, active_coupons} 或 None。"""
    customer = (
        tenant_query(db, Customer, tenant_id).filter(Customer.phone == phone).first()
    )
    if customer is None:
        return None
    from saas_mvp.services import gift_cards as gift_cards_svc
    gift_wallet = gift_cards_svc.customer_wallet(
        db, tenant_id=tenant_id, customer_id=customer.id
    )
    return {
        "customer": customer,
        "points_balance": customer.points_balance or 0,
        "tier": customer.tier or "regular",
        "tier_discount_percent": membership_svc.tier_discount_percent(customer.tier),
        "active_coupons": _active_coupons(db, tenant_id),
        "gift_card_balance_cents": sum(x.balance_cents for x in gift_wallet),
    }


def checkout(
    db: Session,
    *,
    tenant_id: int,
    customer_id: int | None,
    items: list[dict],
    coupon_code: str | None = None,
    points_to_redeem: int = 0,
    reservation_id: int | None = None,
    gift_card_code: str | None = None,
) -> Order:
    """原子結帳：建單 + 折券 + 折點 + 回贈點 + 標到場。任一失敗整筆 rollback。

    items：[{"product_id": int, "qty": int}, ...]。customer_id=None 為散客（walk-in），
    不可折/贈點。
    """
    if not items:
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail="items must not be empty")

    # 合併數量、固定鎖定順序避免死鎖（比照 shop.create_order）。
    merged: dict[int, int] = {}
    for it in items:
        pid = it["product_id"]
        qty = it["qty"]
        if qty <= 0:
            from fastapi import HTTPException

            raise HTTPException(status_code=422, detail="qty must be > 0")
        merged[pid] = merged.get(pid, 0) + qty

    # 顧客列鎖定（點數原子性）；散客 customer 為 None。
    customer: Customer | None = None
    if customer_id is not None:
        customer = db.execute(
            select(Customer)
            .where(Customer.id == customer_id, Customer.tenant_id == tenant_id)
            .with_for_update()
        ).scalar_one_or_none()
        if customer is None:
            raise CustomerNotFound(f"customer {customer_id} not found")

    order = Order(
        tenant_id=tenant_id,
        customer_id=customer.id if customer is not None else None,
        line_user_id=customer.line_user_id if customer is not None else None,
        status=ORDER_PENDING,
        total_cents=0,
        currency=settings.currency,
    )
    db.add(order)
    db.flush()  # 取得 order.id

    subtotal = 0
    for pid in sorted(merged):
        qty = merged[pid]
        product = db.execute(
            select(Product)
            .where(Product.id == pid, Product.tenant_id == tenant_id)
            .with_for_update()
        ).scalar_one_or_none()
        from saas_mvp.services import shop as shop_svc

        if product is None:
            raise shop_svc.ProductNotFound(f"product {pid} not found")
        if not product.is_active:
            raise shop_svc.ProductInactive(f"product {pid} inactive")
        if product.stock is not None and product.stock < qty:
            raise shop_svc.OutOfStock(f"product {pid} out of stock")
        if product.stock is not None:
            product.stock -= qty
        line_total = product.price_cents * qty
        subtotal += line_total
        db.add(OrderItem(
            order_id=order.id,
            product_id=product.id,
            tenant_id=tenant_id,
            name_snapshot=product.name,
            unit_price_cents=product.price_cents,
            qty=qty,
            line_total_cents=line_total,
        ))

    # 會員等級折扣 + 優惠券：三條結帳路徑共用 pricing.apply_order_discounts。
    # 設定 order.discount_cents（等級+券）、order.coupon_code；回傳折後（未扣點）金額。
    from saas_mvp.services import pricing as pricing_svc

    total = pricing_svc.apply_order_discounts(
        db, tenant_id=tenant_id, order=order, customer=customer,
        subtotal_cents=subtotal, line_user_id=None, coupon_code=coupon_code,
    )

    # 折抵點數（不足拋 InsufficientPoints → 整筆 rollback）。
    if points_to_redeem > 0:
        if customer is None:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=422, detail="walk-in cannot redeem points"
            )
        membership_svc.redeem_points(
            db,
            tenant_id=tenant_id,
            customer=customer,
            amount=points_to_redeem,
            reason=f"pos_order:{order.id}",
        )
        total -= points_to_redeem  # 1 點折 1 cent
    total = max(0, total)

    # 禮物卡可分次使用；不足額時 total 保留為現金／刷卡應收。與訂單、庫存、
    # 點數同一交易，任何錯誤整筆 rollback。
    if gift_card_code:
        from saas_mvp.services import gift_cards as gift_cards_svc
        used = gift_cards_svc.redeem_for_order(
            db, tenant_id=tenant_id, code=gift_card_code, order_id=order.id,
            amount_due_cents=total,
            customer_id=customer.id if customer is not None else None,
        )
        order.gift_card_cents = used
        total -= used

    # 對淨付金額回贈點數（散客不贈）。
    if customer is not None and total > 0:
        # 回贈規則：每淨付 100 cents 給 1 點（最少 1）；points_per_booking=0 時停用。
        earn = max(1, total // 100) if settings.points_per_booking > 0 else 0
        if earn > 0:
            membership_svc.earn_points(
                db,
                tenant_id=tenant_id,
                customer=customer,
                delta=earn,
                reason=f"pos_order:{order.id}",
            )

    # 連動預約：標到場（mark_attendance 行內邏輯，同一交易）。
    if reservation_id is not None:
        reservation = (
            tenant_query(db, Reservation, tenant_id)
            .filter(Reservation.id == reservation_id)
            .first()
        )
        if reservation is not None:
            reservation.attended = True

    order.total_cents = total
    db.commit()
    db.refresh(order)
    return order
