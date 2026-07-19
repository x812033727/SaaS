"""公開常駐網路預約 router(R12-A)— /p/{slug}/book,公開、無 token。

* GET  /p/{slug}/book                     — 步驟 1:選服務(無上架服務直接列日期)
* GET  /p/{slug}/book?service_id=&date=   — 步驟 2/3:選日期 → 選員工/時段/人數
* POST /p/{slug}/book                     — 訪客姓名+電話建單(走 public_booking.submit)

漸進式純伺服器渲染(querystring 攜帶步驟狀態,不依賴 JS)。入口三閘
(opt-in + WEB_BOOKING feature + is_published)由 service 判定,未過一律
404 不洩漏存在性。比照 routers/booking_form:公開 + public_limiter 限流。
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
from saas_mvp.services import public_booking as public_booking_svc
from saas_mvp.services.public_booking import PublicBookingError

_PKG_DIR = Path(__file__).resolve().parent.parent  # src/saas_mvp
templates = Jinja2Templates(directory=str(_PKG_DIR / "templates"))

router = APIRouter(
    prefix="/p",
    tags=["public-booking"],
    include_in_schema=False,
    dependencies=[Depends(public_limiter)],
)


def _render(request: Request, slug: str, state: str, **extra) -> HTMLResponse:
    return templates.TemplateResponse(
        "public/book.html",
        {"request": request, "slug": slug, "state": state, **extra},
    )


def _entry_or_404(db: Session, slug: str):
    profile = public_booking_svc.resolve_entry(db, slug)
    if profile is None:
        return None, HTMLResponse(
            "<h1>Not found</h1>", status_code=status.HTTP_404_NOT_FOUND
        )
    return profile, None


@router.get("/{slug}/book", response_class=HTMLResponse)
def public_booking_page(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
    service_id: int | None = None,
    date: str | None = None,
):
    profile, err = _entry_or_404(db, slug)
    if err is not None:
        return err
    tid = profile.tenant_id
    shop_name = profile.display_name or slug

    services = booking_form_svc.active_services(db, tid)
    if services and service_id is None:
        return _render(
            request, slug, "pick_service", shop_name=shop_name, services=services
        )

    if date is None:
        dates = booking_form_svc.available_dates(db, tid)
        return _render(
            request, slug, "pick_date",
            shop_name=shop_name, service_id=service_id, dates=dates,
        )

    slots = booking_form_svc.slots_for(db, tid, date=date, service_id=service_id)
    staff = (
        booking_form_svc.service_staff(db, tid, service_id)
        if service_id is not None else []
    )
    return _render(
        request, slug, "pick_slot",
        shop_name=shop_name,
        service_id=service_id,
        date=date,
        slots=[slot for slot in slots if slot.online_available > 0],
        full_slots=[slot for slot in slots if slot.online_available <= 0],
        staff=staff,
    )


@router.post("/{slug}/book", response_class=HTMLResponse)
def public_booking_submit(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
    slot_id: int = Form(...),
    party_size: int = Form(1),
    service_id: int | None = Form(default=None),
    staff_id: int | None = Form(default=None),
    name: str = Form(default=""),
    phone: str = Form(default=""),
    email: str = Form(default=""),
):
    profile, err = _entry_or_404(db, slug)
    if err is not None:
        return err
    # 表單只提供 1–6;伺服器端同步夾住,防手工 POST 一單掃光整個時段容量。
    party_size = max(1, min(party_size, 6))

    def _err(message: str):
        return _render(
            request, slug, "error",
            message=message, service_id=service_id,
        )

    try:
        resv, customer, created = public_booking_svc.submit(
            db,
            tenant_id=profile.tenant_id,
            slot_id=slot_id,
            party_size=party_size,
            service_id=service_id,
            staff_id=staff_id,
            name=name,
            phone=phone,
            email=email,
        )
    except PublicBookingError as exc:
        return _err(str(exc))
    except booking_svc.SlotFullError:
        return _err("該時段剛剛額滿了,請回上一步改選其他時段。")
    except booking_svc.ResourceUnavailableError:
        return _err("此時段所需的房間或設備已被預約,請改選其他時段。")
    except booking_svc.CustomerBlacklistedError:
        return _err("目前無法完成預約,請直接與店家聯繫。")
    except (booking_svc.SlotNotFoundError, booking_svc.CrossTenantReferenceError):
        return _err("預約資料有誤,請回上一步重新選擇。")

    # 確認信(R12-B,best-effort;book_slot 已 commit,失敗只記 log)。
    public_booking_svc.queue_confirmation_email(db, resv, customer)

    # 定金(照 tenant 既有政策)與待填諮詢表:鏡像 tokenized 表單完成頁。
    deposit_url = None
    deposit_note = None
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
    # portal 連結只發給「本次新建」的顧客檔:併檔到既有客時,輸入他人
    # 電話即可取得該客完整預約歷史(冒名資訊洩漏),故不發。
    portal_url = None
    if created:
        from saas_mvp.services import customer_portal as portal_svc

        portal_url = portal_svc.portal_url(customer)
    return _render(
        request, slug, "done",
        shop_name=profile.display_name or slug,
        reservation=resv,
        deposit_url=deposit_url,
        deposit_note=deposit_note,
        client_form_links=client_form_links,
        portal_url=portal_url,
        visitor_name=(name or "").strip()[:128],
    )
