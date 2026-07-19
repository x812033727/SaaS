"""員工自助入口（public）— 以 access_token 認證的精簡頁面。

prefix /s，include_in_schema=False，**無一般認證**：token 即能力（capability），
由 staff_svc.resolve_by_token 解析（不套租戶 filter）。查無 token → 404。

只揭露該員工自己的即將到來預約、班表、請假——絕不跨員工/跨租戶洩漏。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from saas_mvp.db import get_db
from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.reservation import RESERVATION_CONFIRMED, Reservation
from saas_mvp.services import staff as staff_svc
from saas_mvp.services.tenants import tenant_query

from saas_mvp.auth.ratelimit import public_limiter

# R12-D:公開 token 面補 per-IP 限流(比照 pii/booking_form/customer_portal;
# token 雖不可猜,枚舉噪音與濫用面仍應有第二層閘)。
router = APIRouter(
    prefix="/s",
    tags=["staff-portal"],
    include_in_schema=False,
    dependencies=[Depends(public_limiter)],
)

_PKG_DIR = Path(__file__).resolve().parent.parent  # src/saas_mvp
templates = Jinja2Templates(directory=str(_PKG_DIR / "templates"))


def _resolve_or_404(db: Session, token: str):
    staff = staff_svc.resolve_by_token(db, token)
    if staff is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Not found"
        )
    return staff


def _upcoming_bookings(db: Session, staff) -> list[dict]:
    """該員工即將到來（confirmed）的預約 + 時段時間（限本租戶）。"""
    rows = (
        tenant_query(db, Reservation, staff.tenant_id)
        .filter(
            Reservation.staff_id == staff.id,
            Reservation.status == RESERVATION_CONFIRMED,
        )
        .order_by(Reservation.id)
        .all()
    )
    out = []
    for resv in rows:
        slot = (
            tenant_query(db, BookingSlot, staff.tenant_id)
            .filter(BookingSlot.id == resv.slot_id)
            .first()
        )
        out.append(
            {
                "id": resv.id,
                "slot_start": slot.slot_start if slot else None,
                "party_size": resv.party_size,
            }
        )
    return out


@router.get("/{token}", response_class=HTMLResponse)
def portal_home(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
):
    staff = _resolve_or_404(db, token)
    bookings = _upcoming_bookings(db, staff)
    shifts = staff_svc.list_shifts(
        db, tenant_id=staff.tenant_id, staff_id=staff.id
    )
    leaves = staff_svc.list_leaves(
        db, tenant_id=staff.tenant_id, staff_id=staff.id
    )
    return templates.TemplateResponse(
        request,
        "staff_portal/index.html",
        {
            "staff": staff,
            "bookings": bookings,
            "shifts": shifts,
            "leaves": leaves,
        },
    )


@router.get("/{token}/bookings")
def portal_bookings(
    token: str,
    db: Session = Depends(get_db),
):
    staff = _resolve_or_404(db, token)
    return _upcoming_bookings(db, staff)
