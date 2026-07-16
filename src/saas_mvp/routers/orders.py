"""Orders router — 建單（回 stub 付款連結）、查詢、標付、取消。

受認證 + 租戶隔離 + rate limit；不掛 require_quota。金流為 Stub provider。
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from saas_mvp.deps import get_current_user, get_db, require_rate_limit
from saas_mvp.models.user import User
from saas_mvp.services.features import PRODUCT_SALES, require_feature
from saas_mvp.services.payment import get_payment_provider
from saas_mvp.services.shop import (
    CouponApplyError,
    OrderNotFound,
    OutOfStock,
    ProductInactive,
    ProductNotFound,
    cancel_order,
    create_order,
    get_order,
    list_order_items,
    list_orders,
    mark_order_paid,
)

router = APIRouter(
    prefix="/booking/orders",
    tags=["booking-orders"],
    dependencies=[Depends(require_rate_limit), Depends(require_feature(PRODUCT_SALES))],
)


class OrderItemIn(BaseModel):
    product_id: int
    qty: int = Field(ge=1)


class OrderCreate(BaseModel):
    items: list[OrderItemIn] = Field(min_length=1)
    line_user_id: str | None = Field(default=None, max_length=64)
    coupon_code: str | None = Field(default=None, max_length=64)


class OrderItemResponse(BaseModel):
    product_id: int | None
    name_snapshot: str
    unit_price_cents: int
    qty: int
    line_total_cents: int

    model_config = {"from_attributes": True}


class OrderResponse(BaseModel):
    id: int
    tenant_id: int
    line_user_id: str | None
    status: str
    total_cents: int
    discount_cents: int = 0
    coupon_code: str | None = None
    currency: str
    created_at: datetime.datetime
    paid_at: datetime.datetime | None
    items: list[OrderItemResponse] = []
    checkout_url: str | None = None

    model_config = {"from_attributes": True}


def _with_items(db: Session, tenant_id: int, order) -> OrderResponse:
    resp = OrderResponse.model_validate(order)
    resp.items = [
        OrderItemResponse.model_validate(it)
        for it in list_order_items(db, tenant_id=tenant_id, order_id=order.id)
    ]
    return resp


@router.post("/", response_model=OrderResponse, status_code=status.HTTP_201_CREATED)
def create(
    body: OrderCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OrderResponse:
    try:
        order = create_order(
            db,
            tenant_id=current_user.tenant_id,
            items=[(it.product_id, it.qty) for it in body.items],
            line_user_id=body.line_user_id,
            coupon_code=body.coupon_code,
        )
    except (ProductNotFound,):
        raise HTTPException(status_code=404, detail="Product not found")
    except CouponApplyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except (ProductInactive, OutOfStock) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    resp = _with_items(db, current_user.tenant_id, order)
    resp.checkout_url = get_payment_provider(db).create_checkout(db, order=order)
    return resp


@router.get("/", response_model=list[OrderResponse])
def list_all(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    status_filter: str | None = Query(default=None, alias="status"),
) -> list[OrderResponse]:
    orders = list_orders(
        db, tenant_id=current_user.tenant_id, status_filter=status_filter
    )
    return [_with_items(db, current_user.tenant_id, o) for o in orders]


@router.get("/{order_id}", response_model=OrderResponse)
def get_one(
    order_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OrderResponse:
    try:
        order = get_order(db, tenant_id=current_user.tenant_id, order_id=order_id)
    except OrderNotFound:
        raise HTTPException(status_code=404, detail="Order not found")
    return _with_items(db, current_user.tenant_id, order)


@router.post("/{order_id}/pay", response_model=OrderResponse)
def pay(
    order_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OrderResponse:
    try:
        order = mark_order_paid(db, tenant_id=current_user.tenant_id, order_id=order_id)
    except OrderNotFound:
        raise HTTPException(status_code=404, detail="Order not found")
    return _with_items(db, current_user.tenant_id, order)


@router.post("/{order_id}/cancel", response_model=OrderResponse)
def cancel(
    order_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OrderResponse:
    try:
        order = cancel_order(db, tenant_id=current_user.tenant_id, order_id=order_id)
    except OrderNotFound:
        raise HTTPException(status_code=404, detail="Order not found")
    return _with_items(db, current_user.tenant_id, order)
