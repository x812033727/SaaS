"""商品銷售服務 — 商品 CRUD（純 REST，HTTPException）+ 原子下單（自訂例外，webhook 共用）。

下單原子性：依 product_id 排序後逐一 SELECT … FOR UPDATE 鎖商品列（固定順序避免死鎖），
鎖內驗 is_active 與 stock，扣庫存、快照單價、建 Order+OrderItem、單一 commit。
金額一律整數 cents。
"""

from __future__ import annotations

import datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.models.order import (
    ORDER_CANCELLED,
    ORDER_PAID,
    ORDER_PENDING,
    Order,
)
from saas_mvp.models.order_item import OrderItem
from saas_mvp.models.product import Product
from saas_mvp.services.tenants import tenant_query


# ── 下單例外（REST 轉 HTTP、webhook 轉友善訊息） ──────────────────────────────
class ShopError(Exception):
    pass


class ProductNotFound(ShopError):
    pass


class ProductInactive(ShopError):
    pass


class OutOfStock(ShopError):
    pass


class OrderNotFound(ShopError):
    pass


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# ── 商品 CRUD（純 REST） ──────────────────────────────────────────────────────

def _product_or_404(db: Session, tenant_id: int, product_id: int) -> Product:
    p = tenant_query(db, Product, tenant_id).filter(Product.id == product_id).first()
    if p is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return p


def create_product(
    db: Session,
    *,
    tenant_id: int,
    name: str,
    price_cents: int,
    description: str | None = None,
    stock: int | None = None,
    currency: str | None = None,
) -> Product:
    if price_cents < 0:
        raise HTTPException(status_code=422, detail="price_cents must be >= 0")
    if stock is not None and stock < 0:
        raise HTTPException(status_code=422, detail="stock must be >= 0")
    product = Product(
        tenant_id=tenant_id,
        name=name,
        description=description,
        price_cents=price_cents,
        stock=stock,
        currency=currency or settings.currency,
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


def list_products(
    db: Session, *, tenant_id: int, active_only: bool = False
) -> list[Product]:
    q = tenant_query(db, Product, tenant_id)
    if active_only:
        q = q.filter(Product.is_active.is_(True))
    return q.order_by(Product.id.desc()).all()


def get_product(db: Session, *, tenant_id: int, product_id: int) -> Product:
    return _product_or_404(db, tenant_id, product_id)


def update_product(
    db: Session,
    *,
    tenant_id: int,
    product_id: int,
    name: str | None = None,
    price_cents: int | None = None,
    description: str | None = None,
    stock: int | None = None,
    is_active: bool | None = None,
) -> Product:
    p = _product_or_404(db, tenant_id, product_id)
    if price_cents is not None:
        if price_cents < 0:
            raise HTTPException(status_code=422, detail="price_cents must be >= 0")
        p.price_cents = price_cents
    if name is not None:
        p.name = name
    if description is not None:
        p.description = description
    if stock is not None:
        p.stock = stock
    if is_active is not None:
        p.is_active = is_active
    db.commit()
    db.refresh(p)
    return p


def deactivate_product(db: Session, *, tenant_id: int, product_id: int) -> None:
    p = _product_or_404(db, tenant_id, product_id)
    p.is_active = False
    db.commit()


# ── 訂單 ──────────────────────────────────────────────────────────────────────

def create_order(
    db: Session,
    *,
    tenant_id: int,
    items: list[tuple[int, int]],
    line_user_id: str | None = None,
    customer_id: int | None = None,
) -> Order:
    """原子建單：鎖商品（固定順序）→ 驗庫存 → 扣庫存 + 快照單價 → 建 Order+Items。"""
    if not items:
        raise HTTPException(status_code=422, detail="items must not be empty")

    # 合併同商品數量並依 product_id 排序，固定鎖定順序避免死鎖
    merged: dict[int, int] = {}
    for product_id, qty in items:
        if qty <= 0:
            raise HTTPException(status_code=422, detail="qty must be > 0")
        merged[product_id] = merged.get(product_id, 0) + qty

    order = Order(
        tenant_id=tenant_id,
        line_user_id=line_user_id,
        customer_id=customer_id,
        status=ORDER_PENDING,
        total_cents=0,
        currency=settings.currency,
    )
    db.add(order)
    db.flush()  # 取得 order.id

    total = 0
    for product_id in sorted(merged):
        qty = merged[product_id]
        product = db.execute(
            select(Product)
            .where(Product.id == product_id, Product.tenant_id == tenant_id)
            .with_for_update()
        ).scalar_one_or_none()
        if product is None:
            raise ProductNotFound(f"product {product_id} not found")
        if not product.is_active:
            raise ProductInactive(f"product {product_id} inactive")
        if product.stock is not None and product.stock < qty:
            raise OutOfStock(f"product {product_id} out of stock")
        if product.stock is not None:
            product.stock -= qty
        line_total = product.price_cents * qty
        total += line_total
        db.add(OrderItem(
            order_id=order.id,
            product_id=product.id,
            tenant_id=tenant_id,
            name_snapshot=product.name,
            unit_price_cents=product.price_cents,
            qty=qty,
            line_total_cents=line_total,
        ))

    order.total_cents = total
    db.commit()
    db.refresh(order)
    return order


def get_order(db: Session, *, tenant_id: int, order_id: int) -> Order:
    o = tenant_query(db, Order, tenant_id).filter(Order.id == order_id).first()
    if o is None:
        raise OrderNotFound(f"order {order_id} not found")
    return o


def get_order_by_trade_no(db: Session, merchant_trade_no: str) -> Order | None:
    """依金流唯一交易編號查訂單（金流回調用，不分租戶）；查無回 None。"""
    return db.execute(
        select(Order).where(Order.merchant_trade_no == merchant_trade_no)
    ).scalar_one_or_none()


def list_orders(
    db: Session, *, tenant_id: int, status_filter: str | None = None
) -> list[Order]:
    q = tenant_query(db, Order, tenant_id)
    if status_filter is not None:
        q = q.filter(Order.status == status_filter)
    return q.order_by(Order.id.desc()).all()


def list_order_items(db: Session, *, tenant_id: int, order_id: int) -> list[OrderItem]:
    return (
        tenant_query(db, OrderItem, tenant_id)
        .filter(OrderItem.order_id == order_id)
        .order_by(OrderItem.id)
        .all()
    )


def mark_order_paid(db: Session, *, tenant_id: int, order_id: int) -> Order:
    order = get_order(db, tenant_id=tenant_id, order_id=order_id)
    if order.status == ORDER_PENDING:
        order.status = ORDER_PAID
        order.paid_at = _utcnow()
        db.commit()
        db.refresh(order)
    return order


def cancel_order(db: Session, *, tenant_id: int, order_id: int) -> Order:
    """取消訂單並回補庫存（已取消為 no-op，不重複回補）。"""
    order = get_order(db, tenant_id=tenant_id, order_id=order_id)
    if order.status == ORDER_CANCELLED:
        return order
    # 回補庫存（鎖商品列）
    items = list_order_items(db, tenant_id=tenant_id, order_id=order_id)
    for it in items:
        if it.product_id is None:
            continue
        product = db.execute(
            select(Product)
            .where(Product.id == it.product_id, Product.tenant_id == tenant_id)
            .with_for_update()
        ).scalar_one_or_none()
        if product is not None and product.stock is not None:
            product.stock += it.qty
    order.status = ORDER_CANCELLED
    db.commit()
    db.refresh(order)
    return order
