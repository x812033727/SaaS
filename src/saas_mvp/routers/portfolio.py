"""作品集（portfolio）router — 分類 + 作品 CRUD。

受認證 + 租戶隔離 + rate limit + require_feature(PUBLIC_PROFILE)（折進公開頁旗標，
不另開旗標）。公開消費走 routers/public.py 的店家頁作品集分頁。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from saas_mvp.deps import get_current_user, get_db, require_rate_limit
from saas_mvp.models.user import User
from saas_mvp.services import portfolio as portfolio_svc
from saas_mvp.services.features import PUBLIC_PROFILE, require_feature

router = APIRouter(
    prefix="/booking/portfolio",
    tags=["booking-portfolio"],
    dependencies=[
        Depends(require_rate_limit),
        Depends(require_feature(PUBLIC_PROFILE)),
    ],
)


# ─────────────────────────────── Schemas ─────────────────────────────────────

class CategoryCreate(BaseModel):
    name: str = Field(max_length=128)
    sort_order: int = Field(default=0)


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    sort_order: int | None = None
    is_active: bool | None = None


class CategoryResponse(BaseModel):
    id: int
    tenant_id: int
    name: str
    sort_order: int
    is_active: bool

    model_config = {"from_attributes": True}


class ItemCreate(BaseModel):
    image_url: str = Field(max_length=512)
    category_id: int | None = None
    caption: str | None = None
    sort_order: int = Field(default=0)


class ItemUpdate(BaseModel):
    image_url: str | None = Field(default=None, max_length=512)
    category_id: int | None = None
    caption: str | None = None
    sort_order: int | None = None
    is_active: bool | None = None


class ItemResponse(BaseModel):
    id: int
    tenant_id: int
    category_id: int | None
    image_url: str
    caption: str | None
    sort_order: int
    is_active: bool

    model_config = {"from_attributes": True}


# ─────────────────────────────── Categories ──────────────────────────────────

@router.post(
    "/categories",
    response_model=CategoryResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_category(
    body: CategoryCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CategoryResponse:
    cat = portfolio_svc.create_category(
        db, tenant_id=current_user.tenant_id, name=body.name, sort_order=body.sort_order
    )
    return CategoryResponse.model_validate(cat)


@router.get("/categories", response_model=list[CategoryResponse])
def list_categories(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[CategoryResponse]:
    rows = portfolio_svc.list_categories(db, tenant_id=current_user.tenant_id)
    return [CategoryResponse.model_validate(r) for r in rows]


@router.get("/categories/{category_id}", response_model=CategoryResponse)
def get_category(
    category_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CategoryResponse:
    cat = portfolio_svc.get_category(
        db, tenant_id=current_user.tenant_id, category_id=category_id
    )
    return CategoryResponse.model_validate(cat)


@router.put("/categories/{category_id}", response_model=CategoryResponse)
def update_category(
    category_id: int,
    body: CategoryUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CategoryResponse:
    cat = portfolio_svc.update_category(
        db,
        tenant_id=current_user.tenant_id,
        category_id=category_id,
        name=body.name,
        sort_order=body.sort_order,
        is_active=body.is_active,
    )
    return CategoryResponse.model_validate(cat)


@router.delete(
    "/categories/{category_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_category(
    category_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    portfolio_svc.delete_category(
        db, tenant_id=current_user.tenant_id, category_id=category_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ─────────────────────────────── Items ───────────────────────────────────────

@router.post("/items", response_model=ItemResponse, status_code=status.HTTP_201_CREATED)
def create_item(
    body: ItemCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ItemResponse:
    item = portfolio_svc.create_item(
        db,
        tenant_id=current_user.tenant_id,
        image_url=body.image_url,
        category_id=body.category_id,
        caption=body.caption,
        sort_order=body.sort_order,
    )
    return ItemResponse.model_validate(item)


@router.get("/items", response_model=list[ItemResponse])
def list_items(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    category_id: int | None = Query(default=None),
) -> list[ItemResponse]:
    rows = portfolio_svc.list_items(
        db, tenant_id=current_user.tenant_id, category_id=category_id
    )
    return [ItemResponse.model_validate(r) for r in rows]


@router.get("/items/{item_id}", response_model=ItemResponse)
def get_item(
    item_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ItemResponse:
    item = portfolio_svc.get_item(db, tenant_id=current_user.tenant_id, item_id=item_id)
    return ItemResponse.model_validate(item)


@router.put("/items/{item_id}", response_model=ItemResponse)
def update_item(
    item_id: int,
    body: ItemUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ItemResponse:
    item = portfolio_svc.update_item(
        db,
        tenant_id=current_user.tenant_id,
        item_id=item_id,
        image_url=body.image_url,
        category_id=body.category_id,
        caption=body.caption,
        sort_order=body.sort_order,
        is_active=body.is_active,
    )
    return ItemResponse.model_validate(item)


@router.delete(
    "/items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_item(
    item_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    portfolio_svc.delete_item(db, tenant_id=current_user.tenant_id, item_id=item_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
