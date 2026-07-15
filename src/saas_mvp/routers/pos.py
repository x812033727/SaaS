"""POS router — 電話查會員 + 結帳（PHASE 4-1）。

受認證 + 租戶隔離 + rate limit；因會建立訂單，閘在 PRODUCT_SALES feature 之後。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from saas_mvp.deps import get_current_user, get_db, require_rate_limit
from saas_mvp.models.user import User
from saas_mvp.services import coupons as coupons_svc
from saas_mvp.services import membership as membership_svc
from saas_mvp.services import pos as pos_svc
from saas_mvp.services import shop as shop_svc
from saas_mvp.services import gift_cards as gift_cards_svc
from saas_mvp.services.features import PRODUCT_SALES, require_feature

router = APIRouter(
    prefix="/booking/pos",
    tags=["booking-pos"],
    dependencies=[Depends(require_rate_limit), Depends(require_feature(PRODUCT_SALES))],
)


class CouponBrief(BaseModel):
    id: int
    code: str
    name: str
    discount_type: str
    discount_value: int

    model_config = {"from_attributes": True}


class LookupResponse(BaseModel):
    customer_id: int
    display_name: str | None
    phone: str | None
    points_balance: int
    tier: str
    tier_discount_percent: int = 0
    active_coupons: list[CouponBrief]
    gift_card_balance_cents: int = 0


class CheckoutItem(BaseModel):
    product_id: int
    qty: int = Field(ge=1)


class CheckoutRequest(BaseModel):
    customer_id: int | None = None
    items: list[CheckoutItem] = Field(min_length=1)
    coupon_code: str | None = None
    points_to_redeem: int = Field(default=0, ge=0)
    reservation_id: int | None = None
    gift_card_code: str | None = Field(default=None, max_length=32)


class CheckoutResponse(BaseModel):
    id: int
    tenant_id: int
    customer_id: int | None
    status: str
    total_cents: int
    discount_cents: int = 0
    gift_card_cents: int = 0
    currency: str

    model_config = {"from_attributes": True}


@router.get("/lookup", response_model=LookupResponse)
def lookup(
    phone: str = Query(min_length=1),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> LookupResponse:
    result = pos_svc.lookup_by_phone(
        db, tenant_id=current_user.tenant_id, phone=phone
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    customer = result["customer"]
    return LookupResponse(
        customer_id=customer.id,
        display_name=customer.display_name,
        phone=customer.phone,
        points_balance=result["points_balance"],
        tier=customer.tier or "regular",
        tier_discount_percent=result["tier_discount_percent"],
        active_coupons=[
            CouponBrief.model_validate(c) for c in result["active_coupons"]
        ],
        gift_card_balance_cents=result["gift_card_balance_cents"],
    )


@router.post("/checkout", response_model=CheckoutResponse, status_code=status.HTTP_201_CREATED)
def checkout(
    body: CheckoutRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CheckoutResponse:
    try:
        order = pos_svc.checkout(
            db,
            tenant_id=current_user.tenant_id,
            customer_id=body.customer_id,
            items=[{"product_id": i.product_id, "qty": i.qty} for i in body.items],
            coupon_code=body.coupon_code,
            points_to_redeem=body.points_to_redeem,
            reservation_id=body.reservation_id,
            gift_card_code=body.gift_card_code,
        )
    except pos_svc.CustomerNotFound:
        raise HTTPException(status_code=404, detail="Customer not found")
    except shop_svc.ProductNotFound:
        raise HTTPException(status_code=404, detail="Product not found")
    except shop_svc.ProductInactive:
        raise HTTPException(status_code=409, detail="Product inactive")
    except shop_svc.OutOfStock:
        raise HTTPException(status_code=409, detail="Out of stock")
    except membership_svc.InsufficientPoints:
        raise HTTPException(status_code=409, detail="Insufficient points")
    except coupons_svc.CouponError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except gift_cards_svc.GiftCardError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return CheckoutResponse.model_validate(order)
