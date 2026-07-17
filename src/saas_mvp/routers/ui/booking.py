"""UI 子模組(P4 純搬移自 routers/ui.py):店家自助:預約管理。"""
from __future__ import annotations

import datetime

from fastapi import Depends, Form, Request, status
from fastapi.responses import (
    HTMLResponse,
)
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.deps import (
    Actor,
    get_db,
    require_ui_owner,
    require_ui_user,
)
from saas_mvp.models.tenant import Tenant
from saas_mvp.services import booking as booking_svc
from saas_mvp.services import appointment_series as appointment_series_svc
from saas_mvp.services import deposit as deposit_svc
from saas_mvp.services import waitlist as waitlist_svc
from saas_mvp.services import customers as customers_svc
from saas_mvp.services import audit as audit_svc
from saas_mvp.services import line_config as line_config_svc
from saas_mvp.services import slots as slots_svc
from saas_mvp.services import bookable_resources as resources_svc
from saas_mvp.services.tenants import tenant_query
from fastapi import HTTPException

from saas_mvp.routers.ui._shared import (
    router, templates, _ctx, _line_config_or_none, _opt_int,
)

# ── 店家自助：預約管理 ────────────────────────────────────────────────────────


def _parse_slot_start(value: str) -> datetime.datetime:
    """解析 datetime-local 表單字串（無時區）→ 視為 UTC 的 tz-aware datetime。"""
    dt = datetime.datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _booking_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    """組預約頁 context：bot_mode、時段、預約、顧客。"""
    tid = actor.user.tenant_id
    cfg = _line_config_or_none(db, tid)
    customers = customers_svc.list_customers(db, tenant_id=tid)
    tenant_row = db.query(Tenant).filter(Tenant.id == tid).first()
    booking_slots = slots_svc.list_slots(db, tenant_id=tid)
    reservations = booking_svc.list_reservations(db, tenant_id=tid)
    waitlist_rows = waitlist_svc.list_waitlist(db, tenant_id=tid)
    appointment_series, series_occurrences = appointment_series_svc.list_series(
        db, tenant_id=tid
    )
    occurrence_by_reservation = {
        item.reservation_id: item
        for items in series_occurrences.values()
        for item in items
        if item.reservation_id is not None
    }
    from saas_mvp.models.service_package import PackageCreditLedger

    package_reservation_ids = {
        reservation_id
        for (reservation_id,) in tenant_query(db, PackageCreditLedger, tid)
        .filter(
            PackageCreditLedger.kind == "redeem",
            PackageCreditLedger.reservation_id.is_not(None),
        )
        .with_entities(PackageCreditLedger.reservation_id)
        .all()
    }
    reminder_hours = (
        tenant_row.reminder_hours_before if tenant_row else None
    ) or settings.reminder_hours_before_default
    return _ctx(
        request,
        actor,
        cfg=cfg,
        tenant=tenant_row,
        bot_mode=(cfg or {}).get("bot_mode", "translation"),
        has_line_config=cfg is not None,
        slots=booking_slots,
        reservations=reservations,
        appointment_series=appointment_series,
        series_occurrences=series_occurrences,
        occurrence_by_reservation=occurrence_by_reservation,
        resource_allocations=resources_svc.allocations_for_reservations(
            db,
            tenant_id=tid,
            reservation_ids=[row.id for row in reservations],
        ),
        waitlist_entries=waitlist_rows,
        waitlist_offer_minutes=(
            (tenant_row.waitlist_offer_minutes if tenant_row else None)
            or settings.waitlist_offer_minutes_default
        ),
        slot_by_id={slot.id: slot for slot in booking_slots},
        customers=customers,
        reminder_hours=reminder_hours,
        can_manage_deposits=(
            (getattr(actor.user, "role", None) or "owner") == "owner"
            or actor.user.is_admin
        ),
        can_manage_waitlist_settings=(
            (getattr(actor.user, "role", None) or "owner") == "owner"
            or actor.user.is_admin
        ),
        # 預約列以 customer_id 對應顧客檔，顯示可核對的 LINE 名稱/電話（免額外查詢）。
        customer_by_id={c.id: c for c in customers},
        customer_by_line={c.line_user_id: c for c in customers if c.line_user_id},
        package_reservation_ids=package_reservation_ids,
        **extra,
    )


@router.get("/booking", response_class=HTMLResponse)
def booking_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse("booking.html", _booking_ctx(request, actor, db))


@router.post("/booking/bot-mode", response_class=HTMLResponse)
def booking_set_bot_mode(
    request: Request,
    bot_mode: str = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        line_config_svc.set_bot_mode(db, tid, bot_mode)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_booking_botmode.html", _booking_ctx(request, actor, db, error=error)
    )


@router.post("/booking/deposit-settings", response_class=HTMLResponse)
def booking_set_deposit(
    request: Request,
    deposit_twd: str = Form(""),
    deposit_hold_minutes: str = Form(""),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    """定金設定（C4,owner 限定）:金額(0=停用)與保留分鐘數。"""
    tenant = db.get(Tenant, actor.user.tenant_id)
    error = None
    try:
        amount = int(deposit_twd) if deposit_twd.strip() else 0
        hold = int(deposit_hold_minutes) if deposit_hold_minutes.strip() else None
        if amount < 0 or (hold is not None and hold < 5):
            raise ValueError
        tenant.deposit_cents = amount * 100 if amount else None
        tenant.deposit_hold_minutes = hold
        audit_svc.record_from_actor(
            db,
            actor,
            action="booking.deposit.settings",
            target=f"tenant:{tenant.id}",
            detail={"deposit_twd": amount, "hold_minutes": hold},
            request=request,
        )
        db.commit()
    except ValueError:
        db.rollback()
        error = "金額需為非負整數;保留分鐘數至少 5 分鐘"
    return templates.TemplateResponse(
        "_booking_botmode.html", _booking_ctx(request, actor, db, error=error)
    )


@router.post("/booking/reminder-hours", response_class=HTMLResponse)
def booking_set_reminder_hours(
    request: Request,
    reminder_hours_before: int = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """設定「預約前幾小時提醒」（對標 vibeaico「自訂提醒時間（小時）」）。"""
    tid = actor.user.tenant_id
    error = None
    saved = False
    if reminder_hours_before < 1 or reminder_hours_before > 168:
        error = "提醒時間需介於 1 ～ 168 小時。"
    else:
        tenant_row = db.query(Tenant).filter(Tenant.id == tid).first()
        if tenant_row is not None:
            tenant_row.reminder_hours_before = reminder_hours_before
            db.commit()
            saved = True
    return templates.TemplateResponse(
        "_booking_reminder.html",
        _booking_ctx(request, actor, db, error=error, saved=saved),
    )


@router.post("/booking/waitlist-settings", response_class=HTMLResponse)
def booking_set_waitlist_settings(
    request: Request,
    waitlist_offer_minutes: int = Form(...),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    """候補回應窗口設定；owner 限定。"""
    error = None
    saved = False
    if not 5 <= waitlist_offer_minutes <= 120:
        error = "候補回應時間需介於 5～120 分鐘。"
    else:
        tenant = db.get(Tenant, actor.user.tenant_id)
        tenant.waitlist_offer_minutes = waitlist_offer_minutes
        audit_svc.record_from_actor(
            db,
            actor,
            action="booking.waitlist.settings",
            target=f"tenant:{tenant.id}",
            detail={"offer_minutes": waitlist_offer_minutes},
            request=request,
        )
        db.commit()
        saved = True
    return templates.TemplateResponse(
        "_booking_waitlist.html",
        _booking_ctx(
            request,
            actor,
            db,
            waitlist_error=error,
            waitlist_saved=saved,
        ),
    )


@router.post("/booking/waitlist/{entry_id}/cancel", response_class=HTMLResponse)
def booking_cancel_waitlist_entry(
    request: Request,
    entry_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    error = None
    try:
        waitlist_svc.cancel_waitlist_by_staff(
            db, tenant_id=actor.user.tenant_id, entry_id=entry_id
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="booking.waitlist.cancel",
            target=f"waitlist:{entry_id}",
            request=request,
        )
        db.commit()
    except waitlist_svc.WaitlistEntryNotFound:
        db.rollback()
        error = "候補紀錄不存在。"
    return templates.TemplateResponse(
        "_booking_waitlist.html",
        _booking_ctx(request, actor, db, waitlist_error=error),
    )


@router.post("/booking/slots", response_class=HTMLResponse)
def booking_create_slot(
    request: Request,
    slot_start: str = Form(...),
    max_capacity: int = Form(...),
    walkin_reserved: int = Form(0),
    duration_minutes: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        start = _parse_slot_start(slot_start)
        # 選填時長（分）→ slot_end；供 LINE 引導流程依服務時長過濾時段。
        duration = _opt_int(duration_minutes)
        slot_end = None
        if duration is not None:
            if duration <= 0:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="時長需為正整數（分鐘）",
                )
            slot_end = start + datetime.timedelta(minutes=duration)
        slots_svc.create_slot(
            db,
            tenant_id=tid,
            slot_start=start,
            slot_end=slot_end,
            max_capacity=max_capacity,
            walkin_reserved=walkin_reserved,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "時段時間或時長格式錯誤"
    return templates.TemplateResponse(
        "_booking_slots.html", _booking_ctx(request, actor, db, error=error)
    )


@router.post("/booking/slots/bulk", response_class=HTMLResponse)
def booking_bulk_slots(
    request: Request,
    date_start: str = Form(...),
    date_end: str = Form(...),
    time_start: str = Form(...),
    time_end: str = Form(...),
    interval_minutes: int = Form(...),
    max_capacity: int = Form(...),
    walkin_reserved: int = Form(0),
    weekdays: list[str] = Form(default=[]),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """批次產生時段：日期區間 × 每日營業時間 × 間隔，一鍵展開。"""
    tid = actor.user.tenant_id
    error = None
    bulk_result = None
    try:
        wd = {int(w) for w in weekdays if w.strip() != ""}
        bulk_result = slots_svc.bulk_generate_slots(
            db,
            tenant_id=tid,
            date_start=datetime.date.fromisoformat(date_start),
            date_end=datetime.date.fromisoformat(date_end),
            time_start=datetime.time.fromisoformat(time_start),
            time_end=datetime.time.fromisoformat(time_end),
            interval_minutes=interval_minutes,
            max_capacity=max_capacity,
            walkin_reserved=walkin_reserved,
            weekdays=wd or None,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "日期或時間格式錯誤"
    return templates.TemplateResponse(
        "_booking_slots.html",
        _booking_ctx(request, actor, db, error=error, bulk_result=bulk_result),
    )


@router.get("/booking/slots", response_class=HTMLResponse)
def booking_slots_partial(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """時段列表 partial（編輯列「取消」的 hx-get 目標）。"""
    return templates.TemplateResponse(
        "_booking_slots.html", _booking_ctx(request, actor, db)
    )


@router.get("/booking/slots/{slot_id}/edit", response_class=HTMLResponse)
def booking_edit_slot_form(
    request: Request,
    slot_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "_booking_slots.html",
        _booking_ctx(request, actor, db, editing_slot_id=slot_id),
    )


@router.post("/booking/slots/{slot_id}/update", response_class=HTMLResponse)
def booking_update_slot(
    request: Request,
    slot_id: int,
    max_capacity: int = Form(...),
    walkin_reserved: int = Form(0),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    editing_slot_id = None
    try:
        slots_svc.update_slot(
            db,
            tenant_id=tid,
            slot_id=slot_id,
            max_capacity=max_capacity,
            walkin_reserved=walkin_reserved,
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_slot_id = slot_id  # 失敗時停在編輯列，讓使用者修正
    return templates.TemplateResponse(
        "_booking_slots.html",
        _booking_ctx(request, actor, db, error=error, editing_slot_id=editing_slot_id),
    )


@router.post("/booking/slots/{slot_id}/delete", response_class=HTMLResponse)
def booking_delete_slot(
    request: Request,
    slot_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        slots_svc.delete_slot(db, tenant_id=tid, slot_id=slot_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_booking_slots.html", _booking_ctx(request, actor, db, error=error)
    )


@router.post("/booking/slots/{slot_id}/deactivate", response_class=HTMLResponse)
def booking_deactivate_slot(
    request: Request,
    slot_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        slots_svc.deactivate_slot(db, tenant_id=tid, slot_id=slot_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_booking_slots.html", _booking_ctx(request, actor, db, error=error)
    )


@router.post(
    "/booking/reservations/{reservation_id}/cancel", response_class=HTMLResponse
)
def booking_cancel_reservation(
    request: Request,
    reservation_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=reservation_id)
    except booking_svc.ReservationNotFoundError:
        error = "預約不存在或已取消"
    return templates.TemplateResponse(
        "_booking_reservations.html",
        _booking_ctx(request, actor, db, error=error, refresh_series=True),
    )


@router.post(
    "/booking/reservations/{reservation_id}/series", response_class=HTMLResponse
)
def booking_create_appointment_series(
    request: Request,
    reservation_id: int,
    recurrence_unit: str = Form(...),
    recurrence_interval: int = Form(1),
    occurrence_count: int = Form(...),
    auto_create_slots: bool = Form(False),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    error = None
    series_success = None
    try:
        result = appointment_series_svc.create_from_reservation(
            db,
            tenant_id=actor.user.tenant_id,
            reservation_id=reservation_id,
            recurrence_unit=recurrence_unit,
            recurrence_interval=recurrence_interval,
            occurrence_count=occurrence_count,
            auto_create_slots=auto_create_slots,
            actor_user_id=actor.user.id,
        )
        series_success = (
            f"系列 #{result['series'].id} 已建立：{result['booked']} 筆成功"
            + (f"，{result['conflicts']} 筆衝突待處理。" if result["conflicts"] else "。")
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="booking.series.create",
            target=f"series:{result['series'].id}",
            detail={
                "source_reservation_id": reservation_id,
                "booked": result["booked"],
                "conflicts": result["conflicts"],
            },
            request=request,
        )
        db.commit()
    except appointment_series_svc.AppointmentSeriesError as exc:
        db.rollback()
        error = str(exc)
    return templates.TemplateResponse(
        "_booking_series.html",
        _booking_ctx(
            request,
            actor,
            db,
            series_error=error,
            series_success=series_success,
            refresh_reservations=True,
        ),
    )


@router.post("/booking/series/{series_id}/cancel", response_class=HTMLResponse)
def booking_cancel_appointment_series(
    request: Request,
    series_id: int,
    sequence_from: int = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    error = None
    series_success = None
    try:
        count = appointment_series_svc.cancel_from_sequence(
            db,
            tenant_id=actor.user.tenant_id,
            series_id=series_id,
            sequence_from=sequence_from,
        )
        series_success = f"已取消系列 #{series_id} 自第 {sequence_from} 次起的 {count} 筆有效預約。"
        audit_svc.record_from_actor(
            db,
            actor,
            action="booking.series.cancel_following",
            target=f"series:{series_id}",
            detail={"sequence_from": sequence_from, "cancelled": count},
            request=request,
        )
        db.commit()
    except appointment_series_svc.AppointmentSeriesError as exc:
        db.rollback()
        error = str(exc)
    return templates.TemplateResponse(
        "_booking_series.html",
        _booking_ctx(
            request,
            actor,
            db,
            series_error=error,
            series_success=series_success,
            refresh_reservations=True,
        ),
    )


@router.post(
    "/booking/series/{series_id}/occurrences/{occurrence_id}/retry",
    response_class=HTMLResponse,
)
def booking_retry_series_occurrence(
    request: Request,
    series_id: int,
    occurrence_id: int,
    auto_create_slot: bool = Form(False),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    error = None
    series_success = None
    try:
        result = appointment_series_svc.retry_conflict(
            db,
            tenant_id=actor.user.tenant_id,
            series_id=series_id,
            occurrence_id=occurrence_id,
            auto_create_slot=auto_create_slot,
        )
        if result["booked"]:
            series_success = f"衝突日期已成功建立為預約 #{result['reservation_id']}。"
        else:
            error = f"仍無法建立：{result['reason']}"
        audit_svc.record_from_actor(
            db,
            actor,
            action="booking.series.retry",
            target=f"series_occurrence:{occurrence_id}",
            detail={"result": "booked" if result["booked"] else "conflict"},
            request=request,
        )
        db.commit()
    except appointment_series_svc.AppointmentSeriesError as exc:
        db.rollback()
        error = str(exc)
    return templates.TemplateResponse(
        "_booking_series.html",
        _booking_ctx(
            request,
            actor,
            db,
            series_error=error,
            series_success=series_success,
            refresh_reservations=True,
        ),
    )


@router.post(
    "/booking/reservations/{reservation_id}/deposit-refund",
    response_class=HTMLResponse,
)
def booking_refund_deposit(
    request: Request,
    reservation_id: int,
    amount_twd: int | None = Form(None, ge=1),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    """取消後退還已付定金(可部分,預設全額);owner 限定、服務層鎖列防重。"""
    error = None
    refund_success = None
    try:
        row = deposit_svc.request_full_refund(
            db,
            tenant_id=actor.user.tenant_id,
            reservation_id=reservation_id,
            actor_user_id=actor.user.id,
            amount_cents=amount_twd * 100 if amount_twd is not None else None,
        )
        refunded_twd = (row.deposit_refunded_cents or row.deposit_cents or 0) // 100
        audit_svc.record_from_actor(
            db,
            actor,
            action="booking.deposit.refund",
            target=f"reservation:{reservation_id}",
            detail={
                "result": "refunded",
                "amount_twd": refunded_twd,
                "deposit_twd": (row.deposit_cents or 0) // 100,
                "provider": row.deposit_provider,
            },
            request=request,
        )
        db.commit()
        refund_success = f"預約 #{reservation_id} 定金已退款 NT${refunded_twd}。"
    except deposit_svc.DepositRefundError as exc:
        db.rollback()
        error = str(exc)
        audit_svc.record_from_actor(
            db,
            actor,
            action="booking.deposit.refund",
            target=f"reservation:{reservation_id}",
            detail={"result": "failed", "reason": error},
            request=request,
        )
        db.commit()
    return templates.TemplateResponse(
        "_booking_reservations.html",
        _booking_ctx(request, actor, db, error=error, refund_success=refund_success),
    )


@router.post(
    "/booking/reservations/{reservation_id}/deposit-refund/manual",
    response_class=HTMLResponse,
)
def booking_confirm_manual_deposit_refund(
    request: Request,
    reservation_id: int,
    note: str = Form(..., min_length=2, max_length=200),
    amount_twd: int | None = Form(None, ge=1),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    """外部金流後台已退款後人工對帳(可部分,預設全額);不呼叫金流、不會重複退刷。"""
    error = None
    refund_success = None
    try:
        row = deposit_svc.confirm_manual_refund(
            db,
            tenant_id=actor.user.tenant_id,
            reservation_id=reservation_id,
            actor_user_id=actor.user.id,
            note=note,
            amount_cents=amount_twd * 100 if amount_twd is not None else None,
        )
        refunded_twd = (row.deposit_refunded_cents or row.deposit_cents or 0) // 100
        audit_svc.record_from_actor(
            db,
            actor,
            action="booking.deposit.refund_manual",
            target=f"reservation:{reservation_id}",
            detail={
                "result": "confirmed",
                "amount_twd": refunded_twd,
                "deposit_twd": (row.deposit_cents or 0) // 100,
                "note": note,
            },
            request=request,
        )
        db.commit()
        refund_success = f"預約 #{reservation_id} 已標記為人工退款完成(NT${refunded_twd})。"
    except deposit_svc.DepositRefundError as exc:
        db.rollback()
        error = str(exc)
    return templates.TemplateResponse(
        "_booking_reservations.html",
        _booking_ctx(request, actor, db, error=error, refund_success=refund_success),
    )


@router.post(
    "/booking/reservations/{reservation_id}/attendance", response_class=HTMLResponse
)
def booking_mark_attendance(
    request: Request,
    reservation_id: int,
    attended: str = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        booking_svc.mark_attendance(
            db,
            tenant_id=tid,
            reservation_id=reservation_id,
            attended=(attended == "true"),
        )
    except booking_svc.ReservationNotFoundError:
        error = "預約不存在或已取消"
    return templates.TemplateResponse(
        "_booking_reservations.html", _booking_ctx(request, actor, db, error=error)
    )


