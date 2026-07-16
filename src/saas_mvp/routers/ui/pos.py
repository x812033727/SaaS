"""UI 子模組(P4 純搬移自 routers/ui.py):店家自助:電子禮物卡 + POS 結帳 + 員工抽成與薪資結算。"""
from __future__ import annotations

import csv
import datetime
import io
import secrets
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from fastapi import Depends, Form, Query, Request, status
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.deps import (
    Actor,
    get_db,
    require_ui_owner,
    require_ui_user,
)
from saas_mvp.models.service import Service
from saas_mvp.services import coupons as coupons_svc
from saas_mvp.services import features as features_svc
from saas_mvp.services import customers as customers_svc
from saas_mvp.services import audit as audit_svc
from saas_mvp.services import shop as shop_svc
from saas_mvp.services import staff as staff_svc
from saas_mvp.services import pos as pos_svc
from saas_mvp.services import membership as membership_svc
from saas_mvp.services import gift_cards as gift_cards_svc
from saas_mvp.services import commissions as commissions_svc
from fastapi import HTTPException

from saas_mvp.routers.ui._shared import (
    router, templates, _ctx, _opt_int, _require_ui_feature,
)
from saas_mvp.routers.ui.commerce import _feature_locked

# ── 店家自助：電子禮物卡（GIFT_CARDS） ────────────────────────────────────────


def _gift_cards_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    return _ctx(
        request,
        actor,
        cards=gift_cards_svc.recent_cards(db, tenant_id=tid),
        customers=customers_svc.list_customers(db, tenant_id=tid, limit=300),
        issuance_key=secrets.token_urlsafe(24),
        **extra,
    )


@router.get("/gift-cards", response_class=HTMLResponse)
def gift_cards_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.GIFT_CARDS):
        return _feature_locked(request, actor, features_svc.GIFT_CARDS, "電子禮物卡")
    return templates.TemplateResponse(
        "gift_cards.html", _gift_cards_ctx(request, actor, db)
    )


@router.post("/gift-cards", response_class=HTMLResponse)
def gift_cards_issue(
    request: Request,
    amount_twd: int = Form(...),
    fulfillment_guarantee: str = Form(..., max_length=2000),
    issuance_key: str = Form(..., max_length=64),
    recipient_customer_id: str = Form(""),
    purchaser_name: str = Form("", max_length=128),
    recipient_name: str = Form("", max_length=128),
    message: str = Form("", max_length=500),
    compliance_ack: str = Form(""),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.GIFT_CARDS):
        return _feature_locked(request, actor, features_svc.GIFT_CARDS, "電子禮物卡")
    error = None
    issued_code = None
    issued_card = None
    saved = None
    try:
        if compliance_ack != "true":
            raise gift_cards_svc.GiftCardError("請確認已核對履約保障與禮券法規資訊。")
        result = gift_cards_svc.issue_card(
            db,
            tenant_id=actor.user.tenant_id,
            amount_cents=amount_twd * 100,
            fulfillment_guarantee=fulfillment_guarantee,
            issuance_key=issuance_key,
            issued_by_user_id=actor.user.id,
            recipient_customer_id=_opt_int(recipient_customer_id),
            purchaser_name=purchaser_name,
            recipient_name=recipient_name,
            message=message,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="gift_cards.issue",
            target=f"gift_card:{result.card.id}",
            detail={
                "amount_cents": result.card.initial_value_cents,
                "recipient_customer_id": result.card.recipient_customer_id,
            },
            request=request,
        )
        db.commit()
        issued_code = result.code
        issued_card = result.card if result.created else None
        saved = (
            "禮物卡已發行。" if result.created else "此筆發行已處理，未重複建立禮物卡。"
        )
    except gift_cards_svc.GiftCardError as exc:
        db.rollback()
        error = str(exc)
    return templates.TemplateResponse(
        "_gift_cards.html",
        _gift_cards_ctx(
            request,
            actor,
            db,
            error=error,
            saved=saved,
            issued_code=issued_code,
            issued_card=issued_card,
        ),
    )


@router.post("/gift-cards/{gift_card_id}/void", response_class=HTMLResponse)
def gift_cards_void(
    request: Request,
    gift_card_id: int,
    note: str = Form(..., min_length=2, max_length=255),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.GIFT_CARDS):
        return _feature_locked(request, actor, features_svc.GIFT_CARDS, "電子禮物卡")
    error = None
    try:
        card = gift_cards_svc.void_card(
            db,
            tenant_id=actor.user.tenant_id,
            gift_card_id=gift_card_id,
            note=note,
            actor_user_id=actor.user.id,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="gift_cards.void",
            target=f"gift_card:{card.id}",
            detail={"reason": note},
            request=request,
        )
        db.commit()
    except gift_cards_svc.GiftCardError as exc:
        db.rollback()
        error = str(exc)
    return templates.TemplateResponse(
        "_gift_cards.html", _gift_cards_ctx(request, actor, db, error=error)
    )


# ── 店家自助：POS 結帳（PRODUCT_SALES） ─────────────────────────────────────────


def _pos_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    from saas_mvp.models.booking_slot import BookingSlot
    from saas_mvp.models.order import Order
    from saas_mvp.models.reservation import RESERVATION_CONFIRMED, Reservation

    active_staff = [row for row in staff_svc.list_staff(db, tenant_id=tid) if row.is_active]
    now = datetime.datetime.now(datetime.timezone.utc)
    reservation_rows = db.execute(
        select(Reservation, BookingSlot, Service)
        .join(BookingSlot, BookingSlot.id == Reservation.slot_id)
        .outerjoin(Service, Service.id == Reservation.service_id)
        .outerjoin(Order, Order.reservation_id == Reservation.id)
        .where(
            Reservation.tenant_id == tid,
            Reservation.status == RESERVATION_CONFIRMED,
            BookingSlot.slot_start >= now - datetime.timedelta(days=30),
            Order.id.is_(None),
        )
        .order_by(BookingSlot.slot_start.desc())
        .limit(100)
    ).all()
    return _ctx(
        request,
        actor,
        products=shop_svc.list_products(db, tenant_id=tid),
        gift_cards_enabled=features_svc.is_enabled(db, tid, features_svc.GIFT_CARDS),
        staff=active_staff,
        staff_by_id={row.id: row for row in active_staff},
        pos_reservations=reservation_rows,
        **extra,
    )


@router.get("/pos", response_class=HTMLResponse)
def pos_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PRODUCT_SALES):
        return _feature_locked(request, actor, features_svc.PRODUCT_SALES, "商品銷售")
    return templates.TemplateResponse("pos.html", _pos_ctx(request, actor, db))


@router.post("/pos/lookup", response_class=HTMLResponse)
def pos_lookup(
    request: Request,
    phone: str = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PRODUCT_SALES):
        return _feature_locked(request, actor, features_svc.PRODUCT_SALES, "商品銷售")
    tid = actor.user.tenant_id
    result = pos_svc.lookup_by_phone(db, tenant_id=tid, phone=phone)
    extra = {"lookup_done": True, "phone": phone}
    if result is not None:
        extra.update(
            customer=result["customer"],
            points_balance=result["points_balance"],
            tier_discount_percent=result["tier_discount_percent"],
            active_coupons=result["active_coupons"],
            gift_card_balance_cents=result["gift_card_balance_cents"],
        )
    return templates.TemplateResponse(
        "_pos.html", _pos_ctx(request, actor, db, **extra)
    )


@router.post("/pos/checkout", response_class=HTMLResponse)
async def pos_checkout(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PRODUCT_SALES):
        return _feature_locked(request, actor, features_svc.PRODUCT_SALES, "商品銷售")
    tid = actor.user.tenant_id
    form = await request.form()
    phone = (form.get("phone") or "").strip()
    customer_id = _opt_int(form.get("customer_id") or "")
    coupon_code = (form.get("coupon_code") or "").strip() or None
    gift_card_code = (form.get("gift_card_code") or "").strip() or None
    reservation_id = _opt_int(form.get("reservation_id") or "")
    staff_id = _opt_int(form.get("staff_id") or "")
    payment_method = (form.get("payment_method") or "").strip() or None
    mark_paid = form.get("mark_paid") == "true"
    try:
        tip_cents = int(
            (Decimal(str(form.get("tip_twd") or "0")) * 100).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
    except (InvalidOperation, ValueError):
        tip_cents = -1
    try:
        points_to_redeem = int(form.get("points_to_redeem") or 0)
    except ValueError:
        points_to_redeem = 0

    # 從 qty_<product_id> 欄位組裝結帳明細（數量 > 0 才納入）。
    items: list[dict] = []
    submitted_qty: dict[int, int] = {}
    for key, value in form.items():
        if not key.startswith("qty_"):
            continue
        try:
            product_id = int(key[4:])
            qty = int(value)
        except (TypeError, ValueError):
            continue
        submitted_qty[product_id] = max(0, qty)
        if qty > 0:
            items.append({"product_id": product_id, "qty": qty})

    error = None
    order = None
    if not items and reservation_id is None:
        error = "請選擇一筆預約服務或至少一項商品。"
    else:
        try:
            order = pos_svc.checkout(
                db,
                tenant_id=tid,
                customer_id=customer_id,
                items=items,
                coupon_code=coupon_code,
                points_to_redeem=points_to_redeem,
                gift_card_code=gift_card_code,
                reservation_id=reservation_id,
                staff_id=staff_id,
                payment_method=payment_method,
                tip_cents=tip_cents,
                mark_paid=mark_paid,
            )
        except pos_svc.CustomerNotFound:
            error = "找不到該會員。"
        except shop_svc.ProductNotFound:
            error = "找不到商品。"
        except shop_svc.ProductInactive:
            error = "商品已停用。"
        except shop_svc.OutOfStock:
            error = "庫存不足。"
        except membership_svc.InsufficientPoints:
            error = "點數不足。"
        except coupons_svc.CouponError as exc:
            error = str(exc)
        except gift_cards_svc.GiftCardError as exc:
            error = str(exc)
        except pos_svc.ReservationNotFound:
            error = "找不到該預約，或預約已取消。"
        except pos_svc.ReservationAlreadyCheckedOut:
            error = "這筆預約已經結帳，請勿重複收款。"
        except pos_svc.StaffNotFound:
            error = "找不到指定員工，或員工已停用。"
        except pos_svc.StaffRequired:
            error = "此店已啟用員工抽成，完成收款前請選擇銷售／服務員工。"
        except HTTPException as exc:
            error = str(exc.detail)

    if error is not None:
        # checkout 會先鎖庫存／點數再驗後續條件；任何錯誤都必須整筆回滾。
        db.rollback()

    extra = {"phone": phone}
    if error is not None:
        extra.update(
            selected_reservation_id=reservation_id,
            selected_staff_id=staff_id,
            submitted_qty=submitted_qty,
            submitted_coupon_code=coupon_code or "",
            submitted_points=points_to_redeem,
            submitted_tip_twd=(form.get("tip_twd") or "0"),
            submitted_payment_method=payment_method or "cash",
            submitted_mark_paid=mark_paid,
        )
    if customer_id is not None:
        result = (
            pos_svc.lookup_by_phone(db, tenant_id=tid, phone=phone) if phone else None
        )
        if result is not None:
            extra.update(
                lookup_done=True,
                customer=result["customer"],
                points_balance=result["points_balance"],
                active_coupons=result["active_coupons"],
                gift_card_balance_cents=result["gift_card_balance_cents"],
            )
    return templates.TemplateResponse(
        "_pos.html", _pos_ctx(request, actor, db, order=order, error=error, **extra)
    )


# ── 店家 owner：員工抽成與薪資結算 ───────────────────────────────────────────


def _commissions_ctx(
    request: Request,
    actor: Actor,
    db: Session,
    *,
    pay_run_id: int | None = None,
    **extra,
) -> dict:
    tid = actor.user.tenant_id
    staff = staff_svc.list_staff(db, tenant_id=tid)
    runs = commissions_svc.list_pay_runs(db, tenant_id=tid)
    selected = None
    selected_items = []
    if pay_run_id is not None:
        try:
            selected = commissions_svc.get_pay_run(
                db, tenant_id=tid, pay_run_id=pay_run_id
            )
            selected_items = commissions_svc.pay_run_items(
                db, tenant_id=tid, pay_run_id=selected.id
            )
        except commissions_svc.CommissionError:
            pass
    today = datetime.datetime.now(datetime.timezone.utc).date()
    rules = commissions_svc.latest_rules(db, tenant_id=tid)
    tier_map = {
        rule.id: commissions_svc.rule_tiers(db, tenant_id=tid, rule_id=rule.id)
        for rule in rules.values()
        if rule.structure == "tiered"
    }
    return _ctx(
        request,
        actor,
        staff=staff,
        staff_by_id={row.id: row for row in staff},
        commission_rules=rules,
        commission_tiers=tier_map,
        goal_progress=commissions_svc.sales_goal_progress(
            db, tenant_id=tid, on_date=today
        ),
        pay_runs=runs,
        selected_pay_run=selected,
        selected_pay_run_items=selected_items,
        recent_earnings=commissions_svc.recent_earnings(db, tenant_id=tid),
        today=today,
        month_start=today.replace(day=1),
        **extra,
    )


def _commission_feature_or_locked(request: Request, actor: Actor, db: Session):
    if not _require_ui_feature(db, actor, features_svc.STAFF_COMMISSIONS):
        return _feature_locked(
            request, actor, features_svc.STAFF_COMMISSIONS, "員工抽成／薪資結算"
        )
    return None


def _money_to_cents(raw: str, *, allow_negative: bool = False) -> int:
    try:
        value = Decimal(raw.strip())
    except (InvalidOperation, AttributeError):
        raise commissions_svc.CommissionError("金額格式不正確。") from None
    if not value.is_finite():
        raise commissions_svc.CommissionError("金額格式不正確。")
    if not allow_negative and value < 0:
        raise commissions_svc.CommissionError("金額不可為負數。")
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@router.get("/commissions", response_class=HTMLResponse)
def commissions_page(
    request: Request,
    pay_run_id: int | None = Query(None),
    saved: int = Query(0),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    locked = _commission_feature_or_locked(request, actor, db)
    if locked:
        return locked
    return templates.TemplateResponse(
        "commissions.html",
        _commissions_ctx(
            request, actor, db, pay_run_id=pay_run_id, saved=bool(saved)
        ),
    )


@router.post("/commissions/rules", response_class=HTMLResponse)
def commissions_rule_save(
    request: Request,
    staff_id: int = Form(...),
    item_type: str = Form(...),
    method: str = Form(...),
    value: str = Form(..., max_length=32),
    calculation_basis: str = Form("net"),
    effective_from: datetime.date = Form(...),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    locked = _commission_feature_or_locked(request, actor, db)
    if locked:
        return locked
    try:
        if method == "percent":
            decimal_value = Decimal(value.strip())
            if not decimal_value.is_finite():
                raise commissions_svc.CommissionError("抽成數值格式不正確。")
            stored_value = int(
                (decimal_value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )
        else:
            stored_value = _money_to_cents(value)
        row = commissions_svc.save_rule(
            db,
            tenant_id=actor.user.tenant_id,
            staff_id=staff_id,
            item_type=item_type,
            method=method,
            value=stored_value,
            calculation_basis=calculation_basis,
            effective_from=effective_from,
            actor_user_id=actor.user.id,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="commissions.rule.create",
            target=f"commission_rule:{row.id}",
            detail={"staff_id": staff_id, "item_type": item_type, "effective_from": effective_from.isoformat()},
            request=request,
        )
        db.commit()
    except (InvalidOperation, commissions_svc.CommissionError) as exc:
        db.rollback()
        message = "抽成數值格式不正確。" if isinstance(exc, InvalidOperation) else str(exc)
        return templates.TemplateResponse(
            "commissions.html",
            _commissions_ctx(request, actor, db, error=message),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse("/ui/commissions?saved=1", status_code=303)


@router.post("/commissions/tiered-rules", response_class=HTMLResponse)
async def commissions_tiered_rule_save(
    request: Request,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    locked = _commission_feature_or_locked(request, actor, db)
    if locked:
        return locked
    form = await request.form()
    try:
        staff_id = int(form.get("staff_id") or 0)
        method = str(form.get("method") or "")
        tiers: list[tuple[int, int]] = []
        for index in range(10):
            threshold_raw = str(form.get(f"threshold_{index}") or "").strip()
            value_raw = str(form.get(f"tier_value_{index}") or "").strip()
            if not threshold_raw and not value_raw:
                continue
            if not threshold_raw or not value_raw:
                raise commissions_svc.CommissionError(
                    "每個級距都必須填寫門檻與抽成值。"
                )
            threshold = _money_to_cents(threshold_raw)
            if method == "percent":
                decimal_value = Decimal(value_raw)
                if not decimal_value.is_finite():
                    raise commissions_svc.CommissionError("抽成數值格式不正確。")
                stored_value = int(
                    (decimal_value * 100).quantize(
                        Decimal("1"), rounding=ROUND_HALF_UP
                    )
                )
            else:
                stored_value = _money_to_cents(value_raw)
            tiers.append((threshold, stored_value))
        row = commissions_svc.save_tiered_rule(
            db,
            tenant_id=actor.user.tenant_id,
            staff_id=staff_id,
            item_type=str(form.get("item_type") or ""),
            method=method,
            tiers=tiers,
            calculation_basis=str(form.get("calculation_basis") or "net"),
            sales_period=str(form.get("sales_period") or "monthly"),
            effective_from=datetime.date.fromisoformat(
                str(form.get("effective_from") or "")
            ),
            actor_user_id=actor.user.id,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="commissions.tiered_rule.create",
            target=f"commission_rule:{row.id}",
            detail={"staff_id": staff_id, "tiers": len(tiers)},
            request=request,
        )
        db.commit()
    except (ValueError, InvalidOperation, commissions_svc.CommissionError) as exc:
        db.rollback()
        message = (
            str(exc)
            if isinstance(exc, commissions_svc.CommissionError)
            else "階梯抽成格式不正確。"
        )
        return templates.TemplateResponse(
            "commissions.html",
            _commissions_ctx(request, actor, db, error=message),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse("/ui/commissions?saved=1", status_code=303)


@router.post("/commissions/goals", response_class=HTMLResponse)
def commissions_goal_save(
    request: Request,
    staff_id: int = Form(...),
    item_type: str = Form("all"),
    target_twd: str = Form(..., max_length=32),
    sales_period: str = Form("monthly"),
    effective_from: datetime.date = Form(...),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    locked = _commission_feature_or_locked(request, actor, db)
    if locked:
        return locked
    try:
        goal = commissions_svc.save_sales_goal(
            db,
            tenant_id=actor.user.tenant_id,
            staff_id=staff_id,
            item_type=item_type,
            target_cents=_money_to_cents(target_twd),
            sales_period=sales_period,
            effective_from=effective_from,
            actor_user_id=actor.user.id,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="commissions.goal.create",
            target=f"staff_sales_goal:{goal.id}",
            detail={"staff_id": staff_id, "target_cents": goal.target_cents},
            request=request,
        )
        db.commit()
    except commissions_svc.CommissionError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "commissions.html",
            _commissions_ctx(request, actor, db, error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse("/ui/commissions?saved=1", status_code=303)


def _commission_csv_response(rows: list[list], filename: str) -> Response:
    def safe_cell(value):
        # 避免員工名稱／商品名稱被 Excel 當成公式執行。
        if isinstance(value, str) and value.startswith(
            ("=", "+", "-", "@", "\t", "\r")
        ):
            return "'" + value
        return value

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerows([[safe_cell(cell) for cell in row] for row in rows])
    return Response(
        content=output.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/commissions/pay-runs/{pay_run_id}/export.csv")
def commissions_pay_run_export(
    pay_run_id: int,
    request: Request,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    locked = _commission_feature_or_locked(request, actor, db)
    if locked:
        return locked
    try:
        run, data = commissions_svc.pay_run_export_data(
            db, tenant_id=actor.user.tenant_id, pay_run_id=pay_run_id
        )
    except commissions_svc.CommissionError as exc:
        return Response(str(exc), status_code=status.HTTP_404_NOT_FOUND)
    rows: list[list] = [[
        "結算單", "期間開始", "期間結束", "狀態", "員工",
        "抽成", "小費", "加減項", "應付", "說明",
    ]]
    status_labels = {"draft": "草稿", "finalized": "已確認", "paid": "已付款"}
    for item, staff in data:
        rows.append([
            run.id,
            run.period_start.isoformat(),
            run.period_end.isoformat(),
            status_labels.get(run.status, run.status),
            staff.name if staff else f"員工 #{item.staff_id}",
            f"{item.commission_cents / 100:.2f}",
            f"{item.tip_cents / 100:.2f}",
            f"{item.adjustment_cents / 100:.2f}",
            f"{item.total_cents / 100:.2f}",
            item.adjustment_note or "",
        ])
    return _commission_csv_response(rows, f"pay-run-{run.id}.csv")


@router.get("/commissions/activity.csv")
def commissions_activity_export(
    request: Request,
    period_start: datetime.date = Query(...),
    period_end: datetime.date = Query(...),
    staff_id: str = Query("", max_length=32),
    item_type: str | None = Query(None),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    locked = _commission_feature_or_locked(request, actor, db)
    if locked:
        return locked
    try:
        parsed_staff_id = _opt_int(staff_id)
    except ValueError:
        return Response("員工篩選值不正確。", status_code=status.HTTP_400_BAD_REQUEST)
    try:
        earnings = commissions_svc.activity_export_data(
            db,
            tenant_id=actor.user.tenant_id,
            period_start=period_start,
            period_end=period_end,
            staff_id=parsed_staff_id,
            item_type=item_type,
        )
    except commissions_svc.CommissionError as exc:
        return Response(str(exc), status_code=status.HTTP_400_BAD_REQUEST)
    staff = staff_svc.list_staff(db, tenant_id=actor.user.tenant_id)
    staff_by_id = {row.id: row.name for row in staff}
    rows: list[list] = [[
        "成交時間", "員工", "類型", "項目", "原價",
        "淨額", "抽成／小費", "結算單", "沖銷狀態",
    ]]
    for earning in earnings:
        rows.append([
            earning.earned_at.isoformat(),
            staff_by_id.get(earning.staff_id, f"員工 #{earning.staff_id}"),
            earning.item_type,
            earning.item_name_snapshot,
            f"{earning.gross_cents / 100:.2f}",
            f"{earning.net_cents / 100:.2f}",
            f"{earning.commission_cents / 100:.2f}",
            earning.pay_run_id or "",
            "已沖銷" if earning.reversed_at else "",
        ])
    return _commission_csv_response(
        rows,
        f"commission-activity-{period_start.isoformat()}-{period_end.isoformat()}.csv",
    )


@router.post("/commissions/pay-runs", response_class=HTMLResponse)
def commissions_pay_run_create(
    request: Request,
    period_start: datetime.date = Form(...),
    period_end: datetime.date = Form(...),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    locked = _commission_feature_or_locked(request, actor, db)
    if locked:
        return locked
    try:
        run = commissions_svc.create_pay_run(
            db,
            tenant_id=actor.user.tenant_id,
            period_start=period_start,
            period_end=period_end,
            actor_user_id=actor.user.id,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="commissions.pay_run.create",
            target=f"pay_run:{run.id}",
            detail={"period_start": period_start.isoformat(), "period_end": period_end.isoformat()},
            request=request,
        )
        db.commit()
    except commissions_svc.CommissionError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "commissions.html",
            _commissions_ctx(request, actor, db, error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse(f"/ui/commissions?pay_run_id={run.id}", status_code=303)


@router.post("/commissions/pay-runs/{pay_run_id}/adjust", response_class=HTMLResponse)
def commissions_pay_run_adjust(
    pay_run_id: int,
    request: Request,
    staff_id: int = Form(...),
    adjustment_twd: str = Form("0", max_length=32),
    note: str = Form("", max_length=500),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    locked = _commission_feature_or_locked(request, actor, db)
    if locked:
        return locked
    try:
        row = commissions_svc.update_adjustment(
            db,
            tenant_id=actor.user.tenant_id,
            pay_run_id=pay_run_id,
            staff_id=staff_id,
            adjustment_cents=_money_to_cents(adjustment_twd, allow_negative=True),
            note=note,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="commissions.pay_run.adjust",
            target=f"pay_run:{pay_run_id}",
            detail={"staff_id": staff_id, "adjustment_cents": row.adjustment_cents},
            request=request,
        )
        db.commit()
    except commissions_svc.CommissionError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "commissions.html",
            _commissions_ctx(request, actor, db, pay_run_id=pay_run_id, error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse(f"/ui/commissions?pay_run_id={pay_run_id}", status_code=303)


def _pay_run_transition(
    request: Request,
    actor: Actor,
    db: Session,
    pay_run_id: int,
    action: str,
):
    locked = _commission_feature_or_locked(request, actor, db)
    if locked:
        return locked
    try:
        if action == "finalize":
            commissions_svc.finalize_pay_run(
                db, tenant_id=actor.user.tenant_id, pay_run_id=pay_run_id, actor_user_id=actor.user.id
            )
        elif action == "paid":
            commissions_svc.mark_pay_run_paid(
                db, tenant_id=actor.user.tenant_id, pay_run_id=pay_run_id, actor_user_id=actor.user.id
            )
        else:
            commissions_svc.delete_draft(
                db, tenant_id=actor.user.tenant_id, pay_run_id=pay_run_id
            )
        audit_svc.record_from_actor(
            db,
            actor,
            action=f"commissions.pay_run.{action}",
            target=f"pay_run:{pay_run_id}",
            request=request,
        )
        db.commit()
    except commissions_svc.CommissionError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "commissions.html",
            _commissions_ctx(request, actor, db, pay_run_id=pay_run_id, error=str(exc)),
            status_code=status.HTTP_409_CONFLICT,
        )
    target = (
        "/ui/commissions"
        if action == "delete"
        else f"/ui/commissions?pay_run_id={pay_run_id}"
    )
    return RedirectResponse(target, status_code=303)


@router.post("/commissions/pay-runs/{pay_run_id}/finalize", response_class=HTMLResponse)
def commissions_pay_run_finalize(
    pay_run_id: int,
    request: Request,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    return _pay_run_transition(request, actor, db, pay_run_id, "finalize")


@router.post("/commissions/pay-runs/{pay_run_id}/paid", response_class=HTMLResponse)
def commissions_pay_run_paid(
    pay_run_id: int,
    request: Request,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    return _pay_run_transition(request, actor, db, pay_run_id, "paid")


@router.post("/commissions/pay-runs/{pay_run_id}/delete", response_class=HTMLResponse)
def commissions_pay_run_delete(
    pay_run_id: int,
    request: Request,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    return _pay_run_transition(request, actor, db, pay_run_id, "delete")


