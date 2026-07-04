"""員工排班（staff scheduling）router — 店家端 CRUD + 班表 + 請假 + 指派。

受認證 + 租戶隔離 + rate limit + require_feature(STAFF_SCHEDULING)；不掛 require_quota。
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from saas_mvp.deps import get_current_user, get_db, require_rate_limit
from saas_mvp.models.user import User
from saas_mvp.services import staff as staff_svc
from saas_mvp.services.features import STAFF_SCHEDULING, require_feature

router = APIRouter(
    prefix="/booking/staff",
    tags=["booking-staff"],
    dependencies=[
        Depends(require_rate_limit),
        Depends(require_feature(STAFF_SCHEDULING)),
    ],
)


# ─────────────────────────────── Schemas ─────────────────────────────────────

class StaffCreate(BaseModel):
    name: str = Field(max_length=128)
    role: str | None = Field(default=None, max_length=64)
    location_id: int | None = None
    booking_mode: str = Field(default="capacity", max_length=16)


class StaffUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    role: str | None = Field(default=None, max_length=64)
    location_id: int | None = None
    booking_mode: str | None = Field(default=None, max_length=16)
    is_active: bool | None = None


class StaffResponse(BaseModel):
    id: int
    tenant_id: int
    location_id: int | None
    name: str
    role: str | None
    is_active: bool
    access_token: str | None
    booking_mode: str
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


class ShiftCreate(BaseModel):
    start_time: str = Field(max_length=5, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end_time: str = Field(max_length=5, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    weekday: int | None = Field(default=None, ge=0, le=6)
    rotation: str | None = Field(default=None, max_length=8)


class ShiftUpdate(BaseModel):
    """全欄位選填；weekday=None 表示「不變」（改回每日請走 UI 或重建）。"""

    start_time: str | None = Field(
        default=None, max_length=5, pattern=r"^([01]\d|2[0-3]):[0-5]\d$"
    )
    end_time: str | None = Field(
        default=None, max_length=5, pattern=r"^([01]\d|2[0-3]):[0-5]\d$"
    )
    weekday: int | None = Field(default=None, ge=0, le=6)
    rotation: str | None = Field(default=None, max_length=8)
    is_active: bool | None = None


class ShiftResponse(BaseModel):
    id: int
    tenant_id: int
    staff_id: int
    weekday: int | None
    start_time: str
    end_time: str
    rotation: str | None
    is_active: bool

    model_config = {"from_attributes": True}


class LeaveCreate(BaseModel):
    start_at: datetime.datetime
    end_at: datetime.datetime
    reason: str | None = Field(default=None, max_length=255)
    status: str = Field(default="approved", max_length=16)


class LeaveUpdate(BaseModel):
    start_at: datetime.datetime | None = None
    end_at: datetime.datetime | None = None
    reason: str | None = Field(default=None, max_length=255)
    status: str | None = Field(default=None, max_length=16)


class LeaveResponse(BaseModel):
    id: int
    tenant_id: int
    staff_id: int
    start_at: datetime.datetime
    end_at: datetime.datetime
    reason: str | None
    status: str
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


class AssignBody(BaseModel):
    reservation_id: int


# ─────────────────────────────── Staff CRUD ──────────────────────────────────

@router.post("/", response_model=StaffResponse, status_code=status.HTTP_201_CREATED)
def create(
    body: StaffCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StaffResponse:
    staff = staff_svc.create_staff(
        db,
        tenant_id=current_user.tenant_id,
        name=body.name,
        role=body.role,
        location_id=body.location_id,
        booking_mode=body.booking_mode,
    )
    return StaffResponse.model_validate(staff)


@router.get("/", response_model=list[StaffResponse])
def list_all(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[StaffResponse]:
    rows = staff_svc.list_staff(db, tenant_id=current_user.tenant_id)
    return [StaffResponse.model_validate(r) for r in rows]


@router.get("/{staff_id}", response_model=StaffResponse)
def get_one(
    staff_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StaffResponse:
    staff = staff_svc.get_staff(
        db, tenant_id=current_user.tenant_id, staff_id=staff_id
    )
    return StaffResponse.model_validate(staff)


@router.put("/{staff_id}", response_model=StaffResponse)
def update_one(
    staff_id: int,
    body: StaffUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StaffResponse:
    staff = staff_svc.update_staff(
        db,
        tenant_id=current_user.tenant_id,
        staff_id=staff_id,
        name=body.name,
        role=body.role,
        location_id=body.location_id,
        booking_mode=body.booking_mode,
        is_active=body.is_active,
    )
    return StaffResponse.model_validate(staff)


@router.delete(
    "/{staff_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_one(
    staff_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """刪除員工；其班表/請假/服務指派由 FK ondelete=CASCADE 連帶清除。"""
    staff_svc.delete_staff(
        db, tenant_id=current_user.tenant_id, staff_id=staff_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{staff_id}/rotate-token", response_model=StaffResponse)
def rotate_token(
    staff_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StaffResponse:
    staff = staff_svc.rotate_token(
        db, tenant_id=current_user.tenant_id, staff_id=staff_id
    )
    return StaffResponse.model_validate(staff)


# ─────────────────────────────── Shifts ──────────────────────────────────────

@router.get("/{staff_id}/shifts", response_model=list[ShiftResponse])
def list_shifts(
    staff_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ShiftResponse]:
    rows = staff_svc.list_shifts(
        db, tenant_id=current_user.tenant_id, staff_id=staff_id
    )
    return [ShiftResponse.model_validate(r) for r in rows]


@router.post(
    "/{staff_id}/shifts",
    response_model=ShiftResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_shift(
    staff_id: int,
    body: ShiftCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ShiftResponse:
    shift = staff_svc.create_shift(
        db,
        tenant_id=current_user.tenant_id,
        staff_id=staff_id,
        start_time=body.start_time,
        end_time=body.end_time,
        weekday=body.weekday,
        rotation=body.rotation,
    )
    return ShiftResponse.model_validate(shift)


@router.get("/{staff_id}/shifts/{shift_id}", response_model=ShiftResponse)
def get_shift(
    staff_id: int,
    shift_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ShiftResponse:
    shift = staff_svc.get_shift(
        db, tenant_id=current_user.tenant_id, staff_id=staff_id, shift_id=shift_id
    )
    return ShiftResponse.model_validate(shift)


@router.put("/{staff_id}/shifts/{shift_id}", response_model=ShiftResponse)
def update_shift(
    staff_id: int,
    shift_id: int,
    body: ShiftUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ShiftResponse:
    kwargs: dict = {}
    if body.weekday is not None:
        kwargs["weekday"] = body.weekday
    shift = staff_svc.update_shift(
        db,
        tenant_id=current_user.tenant_id,
        staff_id=staff_id,
        shift_id=shift_id,
        start_time=body.start_time,
        end_time=body.end_time,
        rotation=body.rotation,
        is_active=body.is_active,
        **kwargs,
    )
    return ShiftResponse.model_validate(shift)


@router.delete(
    "/{staff_id}/shifts/{shift_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_shift(
    staff_id: int,
    shift_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    staff_svc.delete_shift(
        db, tenant_id=current_user.tenant_id, staff_id=staff_id, shift_id=shift_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ─────────────────────────────── Leaves ──────────────────────────────────────

@router.get("/{staff_id}/leaves", response_model=list[LeaveResponse])
def list_leaves(
    staff_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[LeaveResponse]:
    rows = staff_svc.list_leaves(
        db, tenant_id=current_user.tenant_id, staff_id=staff_id
    )
    return [LeaveResponse.model_validate(r) for r in rows]


@router.post(
    "/{staff_id}/leaves",
    response_model=LeaveResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_leave(
    staff_id: int,
    body: LeaveCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> LeaveResponse:
    leave = staff_svc.create_leave(
        db,
        tenant_id=current_user.tenant_id,
        staff_id=staff_id,
        start_at=body.start_at,
        end_at=body.end_at,
        reason=body.reason,
        status_value=body.status,
    )
    return LeaveResponse.model_validate(leave)


@router.get("/{staff_id}/leaves/{leave_id}", response_model=LeaveResponse)
def get_leave(
    staff_id: int,
    leave_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> LeaveResponse:
    leave = staff_svc.get_leave(
        db, tenant_id=current_user.tenant_id, staff_id=staff_id, leave_id=leave_id
    )
    return LeaveResponse.model_validate(leave)


@router.put("/{staff_id}/leaves/{leave_id}", response_model=LeaveResponse)
def update_leave(
    staff_id: int,
    leave_id: int,
    body: LeaveUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> LeaveResponse:
    leave = staff_svc.update_leave(
        db,
        tenant_id=current_user.tenant_id,
        staff_id=staff_id,
        leave_id=leave_id,
        start_at=body.start_at,
        end_at=body.end_at,
        reason=body.reason,
        status_value=body.status,
    )
    return LeaveResponse.model_validate(leave)


@router.delete(
    "/{staff_id}/leaves/{leave_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_leave(
    staff_id: int,
    leave_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    staff_svc.delete_leave(
        db, tenant_id=current_user.tenant_id, staff_id=staff_id, leave_id=leave_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ─────────────────────────────── Assignment ──────────────────────────────────

@router.post("/{staff_id}/assign")
def assign(
    staff_id: int,
    body: AssignBody,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        reservation = staff_svc.assign_staff(
            db,
            tenant_id=current_user.tenant_id,
            reservation_id=body.reservation_id,
            staff_id=staff_id,
        )
    except staff_svc.StaffNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Staff or reservation not found"
        )
    except staff_svc.StaffConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        )
    return {
        "reservation_id": reservation.id,
        "staff_id": reservation.staff_id,
        "status": reservation.status,
    }
