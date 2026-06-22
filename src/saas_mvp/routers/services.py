"""服務目錄（service catalog）router — 分類 + 服務項目 + 員工指派 CRUD。

受認證 + 租戶隔離 + rate limit + require_feature(SERVICE_CATALOG)；不掛 require_quota。
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from saas_mvp.deps import get_current_user, get_db, require_rate_limit
from saas_mvp.models.user import User
from saas_mvp.services import catalog as catalog_svc
from saas_mvp.services.features import SERVICE_CATALOG, require_feature

router = APIRouter(
    prefix="/booking/services",
    tags=["booking-services"],
    dependencies=[
        Depends(require_rate_limit),
        Depends(require_feature(SERVICE_CATALOG)),
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


class ServiceCreate(BaseModel):
    name: str = Field(max_length=128)
    category_id: int | None = None
    duration_minutes: int = Field(default=60, ge=0)
    price_cents: int = Field(default=0, ge=0)
    location_id: int | None = None


class ServiceUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    category_id: int | None = None
    duration_minutes: int | None = Field(default=None, ge=0)
    price_cents: int | None = Field(default=None, ge=0)
    location_id: int | None = None
    is_active: bool | None = None


class ServiceResponse(BaseModel):
    id: int
    tenant_id: int
    category_id: int | None
    name: str
    duration_minutes: int
    price_cents: int
    is_active: bool
    location_id: int | None
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


class ServiceStaffBody(BaseModel):
    staff_id: int


class ServiceStaffResponse(BaseModel):
    id: int
    tenant_id: int
    service_id: int
    staff_id: int

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
    cat = catalog_svc.create_category(
        db,
        tenant_id=current_user.tenant_id,
        name=body.name,
        sort_order=body.sort_order,
    )
    return CategoryResponse.model_validate(cat)


@router.get("/categories", response_model=list[CategoryResponse])
def list_categories(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[CategoryResponse]:
    rows = catalog_svc.list_categories(db, tenant_id=current_user.tenant_id)
    return [CategoryResponse.model_validate(r) for r in rows]


@router.get("/categories/{category_id}", response_model=CategoryResponse)
def get_category(
    category_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CategoryResponse:
    cat = catalog_svc.get_category(
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
    cat = catalog_svc.update_category(
        db,
        tenant_id=current_user.tenant_id,
        category_id=category_id,
        name=body.name,
        sort_order=body.sort_order,
        is_active=body.is_active,
    )
    return CategoryResponse.model_validate(cat)


# ─────────────────────────────── Services ────────────────────────────────────

@router.post("/", response_model=ServiceResponse, status_code=status.HTTP_201_CREATED)
def create_service(
    body: ServiceCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ServiceResponse:
    svc = catalog_svc.create_service(
        db,
        tenant_id=current_user.tenant_id,
        name=body.name,
        category_id=body.category_id,
        duration_minutes=body.duration_minutes,
        price_cents=body.price_cents,
        location_id=body.location_id,
    )
    return ServiceResponse.model_validate(svc)


@router.get("/", response_model=list[ServiceResponse])
def list_services(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    location_id: int | None = Query(default=None),
    category_id: int | None = Query(default=None),
) -> list[ServiceResponse]:
    rows = catalog_svc.list_services(
        db,
        tenant_id=current_user.tenant_id,
        location_id=location_id,
        category_id=category_id,
    )
    return [ServiceResponse.model_validate(r) for r in rows]


@router.get("/{service_id}", response_model=ServiceResponse)
def get_service(
    service_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ServiceResponse:
    svc = catalog_svc.get_service(
        db, tenant_id=current_user.tenant_id, service_id=service_id
    )
    return ServiceResponse.model_validate(svc)


@router.put("/{service_id}", response_model=ServiceResponse)
def update_service(
    service_id: int,
    body: ServiceUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ServiceResponse:
    svc = catalog_svc.update_service(
        db,
        tenant_id=current_user.tenant_id,
        service_id=service_id,
        name=body.name,
        category_id=body.category_id,
        duration_minutes=body.duration_minutes,
        price_cents=body.price_cents,
        location_id=body.location_id,
        is_active=body.is_active,
    )
    return ServiceResponse.model_validate(svc)


# ─────────────────────────────── Staff assignment ────────────────────────────

@router.get("/{service_id}/staff", response_model=list[ServiceStaffResponse])
def list_service_staff(
    service_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ServiceStaffResponse]:
    rows = catalog_svc.list_service_staff(
        db, tenant_id=current_user.tenant_id, service_id=service_id
    )
    return [ServiceStaffResponse.model_validate(r) for r in rows]


@router.post(
    "/{service_id}/staff",
    response_model=ServiceStaffResponse,
    status_code=status.HTTP_201_CREATED,
)
def assign_service_staff(
    service_id: int,
    body: ServiceStaffBody,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ServiceStaffResponse:
    link = catalog_svc.assign_staff(
        db,
        tenant_id=current_user.tenant_id,
        service_id=service_id,
        staff_id=body.staff_id,
    )
    return ServiceStaffResponse.model_validate(link)


@router.delete(
    "/{service_id}/staff/{staff_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def unassign_service_staff(
    service_id: int,
    staff_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    catalog_svc.unassign_staff(
        db,
        tenant_id=current_user.tenant_id,
        service_id=service_id,
        staff_id=staff_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
