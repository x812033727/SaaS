"""分店（multi-location）router — 店家端 CRUD。

受認證 + 租戶隔離 + rate limit + require_feature(MULTI_LOCATION)；不掛 require_quota。
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from saas_mvp.deps import get_current_user, get_db, require_rate_limit
from saas_mvp.models.user import User
from saas_mvp.services.features import MULTI_LOCATION, require_feature
from saas_mvp.services.locations import (
    LocationLimitError,
    create_location,
    get_location,
    list_locations,
    update_location,
)

router = APIRouter(
    prefix="/booking/locations",
    tags=["booking-locations"],
    dependencies=[Depends(require_rate_limit), Depends(require_feature(MULTI_LOCATION))],
)


# ─────────────────────────────── Schemas ─────────────────────────────────────

class LocationCreate(BaseModel):
    name: str = Field(max_length=128)
    address: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    timezone: str | None = Field(default=None, max_length=64)


class LocationUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    address: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    timezone: str | None = Field(default=None, max_length=64)
    is_active: bool | None = None


class LocationResponse(BaseModel):
    id: int
    tenant_id: int
    name: str
    address: str | None
    phone: str | None
    timezone: str | None
    is_active: bool
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


# ─────────────────────────────── Endpoints ───────────────────────────────────

@router.post("/", response_model=LocationResponse, status_code=status.HTTP_201_CREATED)
def create(
    body: LocationCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> LocationResponse:
    try:
        location = create_location(
            db,
            tenant_id=current_user.tenant_id,
            name=body.name,
            address=body.address,
            phone=body.phone,
            timezone=body.timezone,
        )
    except LocationLimitError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="已達分店數量上限",
        )
    return LocationResponse.model_validate(location)


@router.get("/", response_model=list[LocationResponse])
def list_all(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[LocationResponse]:
    rows = list_locations(db, tenant_id=current_user.tenant_id)
    return [LocationResponse.model_validate(r) for r in rows]


@router.get("/{location_id}", response_model=LocationResponse)
def get_one(
    location_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> LocationResponse:
    location = get_location(
        db, tenant_id=current_user.tenant_id, location_id=location_id
    )
    return LocationResponse.model_validate(location)


@router.put("/{location_id}", response_model=LocationResponse)
def update_one(
    location_id: int,
    body: LocationUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> LocationResponse:
    location = update_location(
        db,
        tenant_id=current_user.tenant_id,
        location_id=location_id,
        name=body.name,
        address=body.address,
        phone=body.phone,
        timezone=body.timezone,
        is_active=body.is_active,
    )
    return LocationResponse.model_validate(location)


@router.delete(
    "/{location_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response
)
def delete_one(
    location_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """軟刪：停用分店（保留既有關聯）。"""
    update_location(
        db,
        tenant_id=current_user.tenant_id,
        location_id=location_id,
        is_active=False,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
