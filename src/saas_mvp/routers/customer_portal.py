"""顧客自助入口網 router(R5-B1「我的預約」)— 公開、token 即能力。

* GET  /booking/my/{token}                              — 我的預約(未來+近期歷史+候補)
* POST /booking/my/{token}/reservations/{rid}/cancel    — 取消
* POST /booking/my/{token}/reservations/{rid}/confirm   — 確認出席
* GET  /booking/my/{token}/reservations/{rid}/reschedule — 改期:選日期→選時段
* POST /booking/my/{token}/reservations/{rid}/reschedule — 改期送出
* POST /booking/my/{token}/waitlist/{wid}/cancel        — 取消候補(需 LINE 綁定)

比照 routers/booking_form.py:公開 + public_limiter 限流、include_in_schema=False、
純伺服器渲染(PRG:寫入成功 303 回入口頁帶 msg)。token 為長效常駐入口
(services/customer_portal.py),與 booking_form 一次性 token 不同。
無 CSRF hidden field:token 本身即能力(與 booking_form/pii 同準則),
攻擊者無 token 無法構造表單目標 URL。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from saas_mvp.auth.ratelimit import public_limiter
from saas_mvp.db import get_db
from saas_mvp.services import booking as booking_svc
from saas_mvp.services import booking_form as booking_form_svc
from saas_mvp.services import customer_portal as portal_svc
from saas_mvp.services import waitlist as waitlist_svc
from saas_mvp.services.customer_portal import PortalTokenNotFound

_PKG_DIR = Path(__file__).resolve().parent.parent  # src/saas_mvp
templates = Jinja2Templates(directory=str(_PKG_DIR / "templates"))

router = APIRouter(
    prefix="/booking/my",
    tags=["customer-portal"],
    include_in_schema=False,
    dependencies=[Depends(public_limiter)],
)

_NOT_FOUND = HTMLResponse(
    "<h1>連結不存在</h1>", status_code=status.HTTP_404_NOT_FOUND
)

# msg 代碼 → 顯示文字(PRG 用 querystring 傳代碼,不傳自由文字防注入/長度濫用)
_MESSAGES = {
    "cancelled": "預約已取消。",
    "confirmed": "已確認出席,期待您的光臨!",
    "rescheduled": "改期完成。",
    "waitlist_cancelled": "候補已取消。",
    "slot_full": "該時段已額滿,請改選其他時段。",
    "error": "操作未完成,請重試或聯絡店家。",
    "email_saved": "Email 已更新,預約提醒也會寄到您的信箱。",
    "email_invalid": "Email 格式不正確,請重新輸入。",
}


def _redirect(token: str, msg: str) -> RedirectResponse:
    return RedirectResponse(
        f"/booking/my/{token}?msg={msg}", status_code=status.HTTP_303_SEE_OTHER
    )


def _resolve(db: Session, token: str):
    try:
        return portal_svc.resolve_portal_token(db, token)
    except PortalTokenNotFound:
        return None


@router.get("/{token}", response_class=HTMLResponse)
def portal_page(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
    msg: str | None = None,
):
    customer = _resolve(db, token)
    if customer is None:
        return _NOT_FOUND
    data = portal_svc.portal_reservations(db, customer)
    waitlist_rows = []
    if customer.line_user_id:
        entries = waitlist_svc.list_my_waitlist(
            db, tenant_id=customer.tenant_id, line_user_id=customer.line_user_id
        )
        slot_map = {}
        if entries:
            from saas_mvp.models.booking_slot import BookingSlot

            slots = (
                db.query(BookingSlot)
                .filter(BookingSlot.id.in_([e.slot_id for e in entries]))
                .all()
            )
            slot_map = {s.id: s for s in slots}
        waitlist_rows = [
            {"entry": e, "slot": slot_map.get(e.slot_id)} for e in entries
        ]
    return templates.TemplateResponse(
        "customer_portal/portal.html",
        {
            "request": request,
            "token": token,
            "state": "list",
            "customer": customer,
            "upcoming": data["upcoming"],
            "history": data["history"],
            "waitlist": waitlist_rows,
            "message": _MESSAGES.get(msg or ""),
        },
    )


@router.post("/{token}/email", response_class=HTMLResponse)
def portal_set_email(
    token: str,
    db: Session = Depends(get_db),
    email: str = Form(default=""),
):
    """顧客自助填寫/更新 email(R5-B3;提醒第三管道)。空值=清除。"""
    customer = _resolve(db, token)
    if customer is None:
        return _NOT_FOUND
    raw = (email or "").strip()
    if raw:
        cleaned = portal_svc.valid_email(raw)
        if cleaned is None:
            return _redirect(token, "email_invalid")
        customer.email = cleaned
    else:
        customer.email = None
    db.commit()
    return _redirect(token, "email_saved")


@router.post("/{token}/reservations/{rid}/cancel", response_class=HTMLResponse)
def portal_cancel(
    token: str,
    rid: int,
    db: Session = Depends(get_db),
):
    customer = _resolve(db, token)
    if customer is None:
        return _NOT_FOUND
    try:
        booking_svc.cancel_reservation(
            db,
            tenant_id=customer.tenant_id,
            reservation_id=rid,
            customer_id=customer.id,
        )
    except (
        booking_svc.ReservationNotFoundError,
        booking_svc.ReservationPermissionError,
    ):
        return _redirect(token, "error")
    return _redirect(token, "cancelled")


@router.post("/{token}/reservations/{rid}/confirm", response_class=HTMLResponse)
def portal_confirm(
    token: str,
    rid: int,
    db: Session = Depends(get_db),
):
    customer = _resolve(db, token)
    if customer is None:
        return _NOT_FOUND
    try:
        booking_svc.confirm_reservation(
            db,
            tenant_id=customer.tenant_id,
            reservation_id=rid,
            customer_id=customer.id,
        )
    except (
        booking_svc.ReservationNotFoundError,
        booking_svc.ReservationPermissionError,
    ):
        return _redirect(token, "error")
    return _redirect(token, "confirmed")


@router.get(
    "/{token}/reservations/{rid}/reschedule", response_class=HTMLResponse
)
def portal_reschedule_form(
    token: str,
    rid: int,
    request: Request,
    db: Session = Depends(get_db),
    date: str | None = None,
):
    customer = _resolve(db, token)
    if customer is None:
        return _NOT_FOUND
    # 先驗預約歸屬與狀態(唯讀,不動資料):查無/他人 → 回列表帶錯誤。
    data = portal_svc.portal_reservations(db, customer)
    target = next(
        (r for r in data["upcoming"] if r["reservation"].id == rid), None
    )
    if target is None:
        return _redirect(token, "error")
    tid = customer.tenant_id
    if date is None:
        dates = booking_form_svc.available_dates(db, tid)
        return templates.TemplateResponse(
            "customer_portal/portal.html",
            {
                "request": request,
                "token": token,
                "state": "pick_date",
                "customer": customer,
                "target": target,
                "dates": dates,
            },
        )
    slots = booking_form_svc.slots_for(
        db, tid, date=date, service_id=target["reservation"].service_id
    )
    party = target["reservation"].party_size or 1
    return templates.TemplateResponse(
        "customer_portal/portal.html",
        {
            "request": request,
            "token": token,
            "state": "pick_slot",
            "customer": customer,
            "target": target,
            "date": date,
            "slots": [s for s in slots if s.online_available >= party],
        },
    )


@router.post(
    "/{token}/reservations/{rid}/reschedule", response_class=HTMLResponse
)
def portal_reschedule_submit(
    token: str,
    rid: int,
    db: Session = Depends(get_db),
    slot_id: int = Form(...),
):
    customer = _resolve(db, token)
    if customer is None:
        return _NOT_FOUND
    try:
        booking_svc.reschedule_reservation(
            db,
            tenant_id=customer.tenant_id,
            reservation_id=rid,
            new_slot_id=slot_id,
            customer_id=customer.id,
        )
    except booking_svc.SlotFullError:
        return _redirect(token, "slot_full")
    except (
        booking_svc.ReservationNotFoundError,
        booking_svc.ReservationPermissionError,
        booking_svc.SlotNotFoundError,
    ):
        return _redirect(token, "error")
    return _redirect(token, "rescheduled")


@router.post("/{token}/waitlist/{wid}/cancel", response_class=HTMLResponse)
def portal_waitlist_cancel(
    token: str,
    wid: int,
    db: Session = Depends(get_db),
):
    customer = _resolve(db, token)
    if customer is None:
        return _NOT_FOUND
    if not customer.line_user_id:
        return _redirect(token, "error")
    try:
        waitlist_svc.cancel_waitlist(
            db,
            tenant_id=customer.tenant_id,
            entry_id=wid,
            line_user_id=customer.line_user_id,
        )
    except waitlist_svc.WaitlistEntryNotFound:
        return _redirect(token, "error")
    return _redirect(token, "waitlist_cancelled")
