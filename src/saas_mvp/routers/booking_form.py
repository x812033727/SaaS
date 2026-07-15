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
from saas_mvp.services import features as features_svc
from saas_mvp.services.booking_form import (
    TokenAlreadyUsed,
    TokenExpired,
    TokenNotFound,
)
from saas_mvp.services import service_packages as packages_svc

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
    package_credits = 0
    if service_id is not None and features_svc.is_enabled(
        db, tid, features_svc.SERVICE_PACKAGES
    ):
        from saas_mvp.models.customer import Customer

        customer = (
            db.query(Customer)
            .filter(
                Customer.tenant_id == tid,
                Customer.line_user_id == row.line_user_id,
            )
            .first()
        )
        if customer is not None:
            package_credits = packages_svc.eligible_credit_count(
                db,
                tenant_id=tid,
                customer_id=customer.id,
                service_id=service_id,
            )
    return _render_state(
        request, token, "pick_slot",
        service_id=service_id,
        date=date,
        slots=[slot for slot in slots if slot.online_available > 0],
        full_slots=[slot for slot in slots if slot.online_available <= 0],
        staff=staff,
        package_credits=package_credits,
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
    use_package: bool = Form(default=False),
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
            use_package=use_package,
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
            message="該時段剛剛額滿了，可直接加入候補或改選其他時段。",
            service_id=service_id,
            staff_id=staff_id,
            slot_id=slot_id,
            party_size=party_size,
            can_waitlist=True,
        )
    except booking_svc.ResourceUnavailableError:
        return _render_state(
            request,
            token,
            "error",
            message="此時段所需的房間或設備已被預約，請改選其他時段。",
            service_id=service_id,
            staff_id=staff_id,
            party_size=party_size,
        )
    except booking_svc.CustomerBlacklistedError:
        return _render_state(
            request, token, "error",
            message="很抱歉，您目前無法在線上預約，請直接與店家聯繫。",
        )
    except packages_svc.PackageCreditUnavailable:
        return _render_state(
            request,
            token,
            "error",
            message="套票次數已用完或已過期，請取消勾選套票後重新預約。",
            service_id=service_id,
        )
    except (booking_svc.SlotNotFoundError, booking_svc.CrossTenantReferenceError):
        return _render_state(
            request, token, "error",
            message="預約資料有誤，請回上一步重新選擇。",
            service_id=service_id,
        )

    deposit_url = None
    deposit_note = None
    from saas_mvp.models.service_package import PackageCreditLedger

    package_redeemed = (
        db.query(PackageCreditLedger)
        .filter(
            PackageCreditLedger.tenant_id == resv.tenant_id,
            PackageCreditLedger.reservation_id == resv.id,
            PackageCreditLedger.kind == "redeem",
        )
        .first()
        is not None
    )
    if getattr(resv, "deposit_status", None) == "pending":
        from saas_mvp.models.tenant import Tenant
        from saas_mvp.services import deposit as deposit_svc

        tenant = db.get(Tenant, resv.tenant_id)
        if tenant is not None:
            deposit_url = deposit_svc.payment_url(resv)
            deposit_note = deposit_svc.deposit_prompt(resv, tenant)
    from saas_mvp.services import client_forms as client_forms_svc
    client_form_links = [
        {"name": row.template_name_snapshot, "url": client_forms_svc.form_url(row)}
        for row in client_forms_svc.for_reservation(
            db, tenant_id=resv.tenant_id, reservation_id=resv.id
        ) if row.status == "pending"
    ]
    return templates.TemplateResponse(
        "booking_form/done.html",
        {
            "request": request,
            "reservation": resv,
            "deposit_url": deposit_url,
            "deposit_note": deposit_note,
            "package_redeemed": package_redeemed,
            "client_form_links": client_form_links,
        },
    )


@router.post("/{token}/waitlist", response_class=HTMLResponse)
def booking_form_waitlist_submit(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
    slot_id: int = Form(...),
    party_size: int = Form(1),
    service_id: int | None = Form(default=None),
    staff_id: int | None = Form(default=None),
):
    from saas_mvp.services import waitlist as waitlist_svc

    party_size = max(1, min(party_size, 6))
    try:
        entry = booking_form_svc.submit_waitlist(
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
    except waitlist_svc.SlotNotFullError:
        return _render_state(
            request,
            token,
            "error",
            message="這個時段已釋出名額，請回上一步直接預約。",
            service_id=service_id,
        )
    except waitlist_svc.WaitlistSlotNotFound:
        return _render_state(
            request,
            token,
            "error",
            message="候補時段或服務資料已變更，請重新選擇。",
            service_id=service_id,
        )
    return _render_state(
        request,
        token,
        "waitlisted",
        entry=entry,
        service_id=service_id,
    )
