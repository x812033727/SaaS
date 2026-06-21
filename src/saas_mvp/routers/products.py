"""Products router — 店家端商品管理（CRUD）。

受認證 + 租戶隔離 + rate limit；不掛 require_quota。
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from saas_mvp.deps import get_current_user, get_db, require_rate_limit
from saas_mvp.models.user import User
from saas_mvp.services.shop import (
    create_product,
    deactivate_product,
    get_product,
    list_products,
    update_product,
)

router = APIRouter(
    prefix="/booking/products",
    tags=["booking-products"],
    dependencies=[Depends(require_rate_limit)],
)


class ProductCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    price_cents: int = Field(ge=0)
    description: str | None = Field(default=None, max_length=2048)
    stock: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, max_length=8)


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    price_cents: int | None = Field(default=None, ge=0)
    description: str | None = Field(default=None, max_length=2048)
    stock: int | None = Field(default=None, ge=0)
    is_active: bool | None = None


class ProductResponse(BaseModel):
    id: int
    tenant_id: int
    name: str
    description: str | None
    price_cents: int
    currency: str
    stock: int | None
    is_active: bool

    model_config = {"from_attributes": True}


@router.post("/", response_model=ProductResponse, status_code=status.HTTP_201_CREATED)
def create(
    body: ProductCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProductResponse:
    p = create_product(
        db,
        tenant_id=current_user.tenant_id,
        name=body.name,
        price_cents=body.price_cents,
        description=body.description,
        stock=body.stock,
        currency=body.currency,
    )
    return ProductResponse.model_validate(p)


@router.get("/", response_model=list[ProductResponse])
def list_all(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    active_only: bool = False,
) -> list[ProductResponse]:
    return [
        ProductResponse.model_validate(p)
        for p in list_products(db, tenant_id=current_user.tenant_id, active_only=active_only)
    ]


@router.get("/{product_id}", response_model=ProductResponse)
def get_one(
    product_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProductResponse:
    return ProductResponse.model_validate(
        get_product(db, tenant_id=current_user.tenant_id, product_id=product_id)
    )


@router.put("/{product_id}", response_model=ProductResponse)
def update_one(
    product_id: int,
    body: ProductUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProductResponse:
    p = update_product(
        db,
        tenant_id=current_user.tenant_id,
        product_id=product_id,
        name=body.name,
        price_cents=body.price_cents,
        description=body.description,
        stock=body.stock,
        is_active=body.is_active,
    )
    return ProductResponse.model_validate(p)


@router.delete(
    "/{product_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response
)
def delete_one(
    product_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    deactivate_product(db, tenant_id=current_user.tenant_id, product_id=product_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
