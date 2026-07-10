"""網頁預約表單 router（A1.1）— 公開、token 即能力、include_in_schema=False。

* GET  /booking/f/{token}                     — 步驟 1：選服務（無上架服務直接列日期）
* GET  /booking/f/{token}?service_id=&date=   — 步驟 2/3：選日期 → 選員工/時段/人數
* POST /booking/f/{token}                     — 建單（走既有 booking.book_slot）

漸進式純伺服器渲染（querystring 攜帶步驟狀態，不依賴 JS），LINE 內建瀏覽器
可直接使用。比照 routers/pii.py：公開 + public_limiter 限流。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from saas_mvp.auth.ratelimit import public_limiter
from saas_mvp.db import get_db
from saas_mvp.services import booking as booking_svc
from saas_mvp.services import booking_form as booking_form_svc
from saas_mvp.services.booking_form import (
    TokenAlreadyUsed,
    TokenExpired,
    TokenNotFound,
)

_PKG_DIR = Path(__file__).resolve().parent.parent  # src/saas_mvp
templates = Jinja2Templates(directory=str(_PKG_DIR / "templates"))

router = APIRouter(
    prefix="/booking/f",
    tags=["booking-form"],
    include_in_schema=False,
    dependencies=[Depends(public_limiter)],
)


def _render_state(request: Request, token: str, state: str, **extra) -> HTMLResponse:
    return templates.TemplateResponse(
        "booking_form/form.html",
        {"request": request, "token": token, "state": state, **extra},
    )


def _resolve_or_page(request: Request, db: Session, token: str) -> tuple:
    """解析 token；異常回 (None, 對應狀態頁)。"""
    try:
        return booking_form_svc.resolve_token(db, token), None
    except TokenNotFound:
        return None, HTMLResponse(
            "<h1>連結不存在</h1>", status_code=status.HTTP_404_NOT_FOUND
        )
    except TokenExpired:
        return None, _render_state(request, token, "expired")
    except TokenAlreadyUsed:
        return None, _render_state(request, token, "used")


@router.get("/{token}", response_class=HTMLResponse)
def booking_form_page(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
    service_id: int | None = None,
    date: str | None = None,
):
    row, err = _resolve_or_page(request, db, token)
    if err is not None:
        return err
    tid = row.tenant_id

    services = booking_form_svc.active_services(db, tid)
    # 步驟 1：選服務（有上架服務且尚未選）。無服務的店直接進日期。
    if services and service_id is None:
        return _render_state(request, token, "pick_service", services=services)

    # 步驟 2：選日期。
    if date is None:
        dates = booking_form_svc.available_dates(db, tid)
        return _render_state(
            request, token, "pick_date",
            service_id=service_id, dates=dates,
        )

    # 步驟 3：選員工（選填）/ 時段 / 人數。
    slots = booking_form_svc.slots_for(db, tid, date=date, service_id=service_id)
    staff = (
        booking_form_svc.service_staff(db, tid, service_id)
        if service_id is not None else []
    )
    return _render_state(
        request, token, "pick_slot",
        service_id=service_id, date=date, slots=slots, staff=staff,
    )


@router.post("/{token}", response_class=HTMLResponse)
def booking_form_submit(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
    slot_id: int = Form(...),
    party_size: int = Form(1),
    service_id: int | None = Form(default=None),
    staff_id: int | None = Form(default=None),
):
    if party_size < 1:
        party_size = 1
    try:
        resv = booking_form_svc.submit_booking(
            db,
            token=token,
            slot_id=slot_id,
            party_size=party_size,
            service_id=service_id,
            staff_id=staff_id,
        )
    except TokenNotFound:
        return HTMLResponse(
            "<h1>連結不存在</h1>", status_code=status.HTTP_404_NOT_FOUND
        )
    except TokenExpired:
        return _render_state(request, token, "expired")
    except TokenAlreadyUsed:
        return _render_state(request, token, "used")
    except booking_svc.SlotFullError:
        return _render_state(
            request, token, "error",
            message="該時段剛剛額滿了，請回上一步改選其他時段（或回 LINE 加入候補）。",
            service_id=service_id,
        )
    except booking_svc.CustomerBlacklistedError:
        return _render_state(
            request, token, "error",
            message="很抱歉，您目前無法在線上預約，請直接與店家聯繫。",
        )
    except (booking_svc.SlotNotFoundError, booking_svc.CrossTenantReferenceError):
        return _render_state(
            request, token, "error",
            message="預約資料有誤，請回上一步重新選擇。",
            service_id=service_id,
        )

    deposit_url = None
    deposit_note = None
    if getattr(resv, "deposit_status", None) == "pending":
        from saas_mvp.models.tenant import Tenant
        from saas_mvp.services import deposit as deposit_svc

        tenant = db.get(Tenant, resv.tenant_id)
        if tenant is not None:
            deposit_url = deposit_svc.payment_url(resv)
            deposit_note = deposit_svc.deposit_prompt(resv, tenant)
    return templates.TemplateResponse(
        "booking_form/done.html",
        {
            "request": request,
            "reservation": resv,
            "deposit_url": deposit_url,
            "deposit_note": deposit_note,
        },
    )
