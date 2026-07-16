"""UI 子模組(P4 純搬移自 routers/ui.py):店家自助:顧客 CRM + CSV 匯入/匯出。"""
from __future__ import annotations

import secrets

from fastapi import Depends, Form, Query, Request, status
from fastapi.responses import (
    HTMLResponse,
    Response,
)
from sqlalchemy.orm import Session

from saas_mvp.deps import (
    Actor,
    get_db,
    require_ui_owner,
    require_ui_user,
)
from saas_mvp.models.service import Service
from saas_mvp.services import booking as booking_svc
from saas_mvp.services import features as features_svc
from saas_mvp.services import customers as customers_svc
from saas_mvp.services import audit as audit_svc
from saas_mvp.services import shop as shop_svc
from saas_mvp.services import catalog as catalog_svc
from saas_mvp.services import membership as membership_svc
from saas_mvp.services import service_packages as packages_svc
from saas_mvp.services import gift_cards as gift_cards_svc
from saas_mvp.services import client_forms as client_forms_svc
from saas_mvp.services import segments as segments_svc
from saas_mvp.services.tenants import tenant_query
from fastapi import HTTPException

from saas_mvp.routers.ui._shared import (
    router, templates, _ctx, _is_htmx, _require_ui_feature,
)
from saas_mvp.routers.ui.customers import _customers_admin_ctx
from saas_mvp.routers.ui.commerce import _feature_locked

# ── 店家自助：顧客 CRM ─────────────────────────────────────────────────────────

_CUSTOMERS_PAGE_SIZE = 20


def _customers_ctx(
    request: Request,
    actor: Actor,
    db: Session,
    *,
    q: str = "",
    page: int = 1,
    **extra,
) -> dict:
    tid = actor.user.tenant_id
    total = customers_svc.count_customers(db, tenant_id=tid, q=q or None)
    pages = max(1, -(-total // _CUSTOMERS_PAGE_SIZE))  # ceil
    page = min(max(1, page), pages)
    rows = customers_svc.list_customers(
        db,
        tenant_id=tid,
        q=q or None,
        limit=_CUSTOMERS_PAGE_SIZE,
        offset=(page - 1) * _CUSTOMERS_PAGE_SIZE,
    )
    return _ctx(
        request,
        actor,
        customers=rows,
        q=q,
        page=page,
        pages=pages,
        total=total,
        **extra,
    )


def _customer_detail_ctx(
    request: Request, actor: Actor, db: Session, customer_id: int, **extra
) -> dict:
    from saas_mvp.models.booking_slot import BookingSlot
    from saas_mvp.models.point_transaction import PointTransaction

    tid = actor.user.tenant_id
    customer = customers_svc.get_customer(
        db, tenant_id=tid, customer_id=customer_id
    )  # 查無/跨租戶 → HTTPException 404
    all_tags = segments_svc.list_tags(db, tenant_id=tid)
    customer_tag_ids = {
        t.id
        for t in segments_svc.list_tags_for_customer(
            db, tenant_id=tid, customer_id=customer_id
        )
    }
    reservations = booking_svc.list_reservations(
        db, tenant_id=tid, line_user_id=customer.line_user_id
    )[-20:][::-1]  # 近 20 筆，新→舊
    slot_ids = [r.slot_id for r in reservations if r.slot_id is not None]
    slots = {}
    if slot_ids:
        slots = {
            s.id: s
            for s in tenant_query(db, BookingSlot, tid)
            .filter(BookingSlot.id.in_(slot_ids))
            .all()
        }
    ledger = (
        tenant_query(db, PointTransaction, tid)
        .filter(PointTransaction.customer_id == customer_id)
        .order_by(PointTransaction.id.desc())
        .limit(20)
        .all()
    )
    packages_enabled = features_svc.is_enabled(db, tid, features_svc.SERVICE_PACKAGES)
    package_wallet = (
        packages_svc.customer_wallet(
            db, tenant_id=tid, customer_id=customer_id, include_empty=True
        )
        if packages_enabled
        else []
    )
    package_ledger = (
        packages_svc.ledger_for_customer(db, tenant_id=tid, customer_id=customer_id)
        if packages_enabled
        else []
    )
    package_services = {
        service.id: service for service in tenant_query(db, Service, tid).all()
    }
    gift_cards_enabled = features_svc.is_enabled(db, tid, features_svc.GIFT_CARDS)
    gift_card_wallet = (
        gift_cards_svc.customer_wallet(db, tenant_id=tid, customer_id=customer_id)
        if gift_cards_enabled
        else []
    )
    client_forms_enabled = features_svc.is_enabled(db, tid, features_svc.CLIENT_FORMS)
    client_form_requests = (
        client_forms_svc.for_customer(db, tenant_id=tid, customer_id=customer_id)
        if client_forms_enabled
        else []
    )
    return _ctx(
        request,
        actor,
        customer=customer,
        all_tags=all_tags,
        customer_tag_ids=customer_tag_ids,
        reservations=reservations,
        slots=slots,
        ledger=ledger,
        packages_enabled=packages_enabled,
        package_wallet=package_wallet,
        package_ledger=package_ledger,
        package_services=package_services,
        available_packages=(
            packages_svc.list_packages(db, tenant_id=tid, active_only=True)
            if packages_enabled
            else []
        ),
        can_issue_packages=(
            (getattr(actor.user, "role", None) or "owner") == "owner"
            or actor.user.is_admin
        ),
        package_issue_key=secrets.token_urlsafe(24),
        gift_cards_enabled=gift_cards_enabled,
        gift_card_wallet=gift_card_wallet,
        client_forms_enabled=client_forms_enabled,
        client_form_requests=client_form_requests,
        client_form_url=client_forms_svc.form_url,
        can_manage_client_forms=(
            (getattr(actor.user, "role", None) or "owner") == "owner"
            or actor.user.is_admin
        ),
        **extra,
    )


@router.post(
    "/customers/{customer_id}/reservations/{reservation_id}/client-forms",
    response_class=HTMLResponse,
)
def customer_attach_client_forms(
    request: Request,
    customer_id: int,
    reservation_id: int,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    from saas_mvp.models.reservation import Reservation

    if not _require_ui_feature(db, actor, features_svc.CLIENT_FORMS):
        return _feature_locked(
            request, actor, features_svc.CLIENT_FORMS, "顧客表單／同意書"
        )
    reservation = tenant_query(db, Reservation, actor.user.tenant_id).filter(
        Reservation.id == reservation_id,
        Reservation.customer_id == customer_id,
    ).one_or_none()
    if reservation is None:
        raise HTTPException(status_code=404, detail="reservation not found")
    rows = client_forms_svc.attach_to_reservation(db, reservation=reservation)
    audit_svc.record_from_actor(
        db,
        actor,
        action="client_forms.attach",
        target=f"reservation:{reservation.id}",
        detail={"forms": len(rows), "customer_id": customer_id},
        request=request,
    )
    db.commit()
    message = (
        f"已確認預約 #{reservation.id} 的適用表單，共 {len(rows)} 份。"
        if rows
        else "目前沒有已啟用且適用此服務的表單。"
    )
    return templates.TemplateResponse(
        "_customer_detail.html",
        _customer_detail_ctx(request, actor, db, customer_id, saved=message),
    )


@router.post("/customers/{customer_id}/gift-cards/claim", response_class=HTMLResponse)
def customer_claim_gift_card(
    request: Request,
    customer_id: int,
    code: str = Form(..., max_length=32),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    error = None
    saved = None
    if not _require_ui_feature(db, actor, features_svc.GIFT_CARDS):
        return _feature_locked(request, actor, features_svc.GIFT_CARDS, "電子禮物卡")
    try:
        card = gift_cards_svc.claim_card(
            db, tenant_id=actor.user.tenant_id, code=code, customer_id=customer_id
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="gift_cards.claim",
            target=f"gift_card:{card.id}",
            detail={"customer_id": customer_id},
            request=request,
        )
        db.commit()
        saved = "禮物卡已加入顧客錢包。"
    except gift_cards_svc.GiftCardError as exc:
        db.rollback()
        error = str(exc)
    return templates.TemplateResponse(
        "_customer_detail.html",
        _customer_detail_ctx(request, actor, db, customer_id, error=error, saved=saved),
    )


def _packages_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    package_rows = packages_svc.list_packages(db, tenant_id=tid)
    services = catalog_svc.list_services(db, tenant_id=tid)
    return _ctx(
        request,
        actor,
        packages=package_rows,
        services=services,
        service_by_id={service.id: service for service in services},
        items_by_package={
            package.id: packages_svc.package_items(
                db, tenant_id=tid, package_id=package.id
            )
            for package in package_rows
        },
        can_manage=(
            (getattr(actor.user, "role", None) or "owner") == "owner"
            or actor.user.is_admin
        ),
        **extra,
    )


@router.get("/packages", response_class=HTMLResponse)
def packages_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.SERVICE_PACKAGES):
        return _feature_locked(
            request, actor, features_svc.SERVICE_PACKAGES, "服務套票"
        )
    return templates.TemplateResponse(
        "packages.html", _packages_ctx(request, actor, db)
    )


@router.post("/packages", response_class=HTMLResponse)
def packages_create(
    request: Request,
    name: str = Form(..., max_length=128),
    description: str = Form("", max_length=2000),
    price_twd: int = Form(...),
    validity_days: int = Form(...),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.SERVICE_PACKAGES):
        return _feature_locked(
            request, actor, features_svc.SERVICE_PACKAGES, "服務套票"
        )
    error = None
    try:
        row = packages_svc.create_package(
            db,
            tenant_id=actor.user.tenant_id,
            name=name,
            description=description,
            price_cents=price_twd * 100,
            validity_days=validity_days,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="packages.create",
            target=f"package:{row.id}",
            detail={
                "price_cents": row.price_cents,
                "validity_days": row.validity_days,
            },
            request=request,
        )
        db.commit()
    except packages_svc.ServicePackageError as exc:
        db.rollback()
        error = str(exc)
    return templates.TemplateResponse(
        "_packages.html", _packages_ctx(request, actor, db, error=error)
    )


@router.post("/packages/{package_id}/items", response_class=HTMLResponse)
def packages_add_item(
    request: Request,
    package_id: int,
    service_id: int = Form(...),
    included_quantity: int = Form(...),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.SERVICE_PACKAGES):
        return _feature_locked(
            request, actor, features_svc.SERVICE_PACKAGES, "服務套票"
        )
    error = None
    try:
        packages_svc.add_or_update_item(
            db,
            tenant_id=actor.user.tenant_id,
            package_id=package_id,
            service_id=service_id,
            included_quantity=included_quantity,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="packages.item.update",
            target=f"package:{package_id}",
            detail={"service_id": service_id, "quantity": included_quantity},
            request=request,
        )
        db.commit()
    except packages_svc.ServicePackageError as exc:
        db.rollback()
        error = str(exc)
    return templates.TemplateResponse(
        "_packages.html", _packages_ctx(request, actor, db, error=error)
    )


@router.post("/packages/{package_id}/active", response_class=HTMLResponse)
def packages_set_active(
    request: Request,
    package_id: int,
    active: str = Form(...),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.SERVICE_PACKAGES):
        return _feature_locked(
            request, actor, features_svc.SERVICE_PACKAGES, "服務套票"
        )
    error = None
    try:
        packages_svc.set_active(
            db,
            tenant_id=actor.user.tenant_id,
            package_id=package_id,
            active=active == "true",
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="packages.active",
            target=f"package:{package_id}",
            detail={"active": active == "true"},
            request=request,
        )
        db.commit()
    except packages_svc.ServicePackageError as exc:
        db.rollback()
        error = str(exc)
    return templates.TemplateResponse(
        "_packages.html", _packages_ctx(request, actor, db, error=error)
    )


@router.get("/customers", response_class=HTMLResponse)
def customers_page(
    request: Request,
    q: str = Query(default="", max_length=64),
    page: int = Query(default=1, ge=1),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    ctx = _customers_ctx(request, actor, db, q=q, page=page)
    if _is_htmx(request):
        return templates.TemplateResponse("_customers_list.html", ctx)
    return templates.TemplateResponse("customers.html", ctx)


# ── 顧客 CSV 匯入 / 匯出 ──────────────────────────────────────────────────────
# 註：/customers/import 與 /customers/export.csv 必須宣告於
# /customers/{customer_id} 之前，否則 "import"/"export.csv" 會被當成 id。


@router.post("/customers/import", response_class=HTMLResponse)
async def customers_import(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """顧客 CSV 批次匯入（multipart；all-or-nothing，錯誤整批不寫）。"""
    # 注意:request.form() 產生的是 starlette 的 UploadFile(fastapi.UploadFile
    # 是其子類,isinstance 檢查必須用 starlette 基類)。
    from starlette.datastructures import UploadFile

    from saas_mvp.services import customer_import as import_svc

    tid = actor.user.tenant_id
    form = await request.form()
    upload = form.get("file")
    update_existing = bool(form.get("update_existing"))
    if upload is None or not isinstance(upload, UploadFile):
        report = import_svc.ImportReport(errors=["請選擇 CSV 檔案"])
    else:
        content = await upload.read()
        report = import_svc.import_customers(
            db, tenant_id=tid, content=content, update_existing=update_existing
        )
    ctx = _customers_ctx(request, actor, db, import_report=report)
    return templates.TemplateResponse("_customers_list.html", ctx)


def _csv_response(rows: list[dict], fieldnames: list[str], filename: str) -> Response:
    import csv as _csv
    import io as _io

    buf = _io.StringIO()
    writer = _csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/customers/export.csv")
def customers_export(
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
) -> Response:
    """顧客匯出（欄位為匯入格式超集，round-trip 相容）。"""
    rows = [
        {
            "display_name": c.display_name or "",
            "phone": c.phone or "",
            "birthday": c.birthday.isoformat() if c.birthday else "",
            "note": c.note or "",
            "line_user_id": c.line_user_id or "",
            "points_balance": c.points_balance,
            "tier": c.tier,
            "booking_count": c.booking_count,
            "last_booked_at": c.last_booked_at.isoformat() if c.last_booked_at else "",
            "created_at": c.created_at.isoformat() if c.created_at else "",
        }
        for c in customers_svc.list_customers(db, tenant_id=actor.user.tenant_id)
    ]
    return _csv_response(
        rows,
        [
            "display_name",
            "phone",
            "birthday",
            "note",
            "line_user_id",
            "points_balance",
            "tier",
            "booking_count",
            "last_booked_at",
            "created_at",
        ],
        "customers.csv",
    )


@router.get("/products/export.csv")
def products_export(
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
) -> Response:
    rows = [
        {
            "name": p.name,
            "price_cents": p.price_cents,
            "stock": "" if p.stock is None else p.stock,
            "is_active": "yes" if p.is_active else "no",
            "description": p.description or "",
        }
        for p in shop_svc.list_products(db, tenant_id=actor.user.tenant_id)
    ]
    return _csv_response(
        rows,
        ["name", "price_cents", "stock", "is_active", "description"],
        "products.csv",
    )


@router.get("/services/export.csv")
def services_export(
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
) -> Response:
    rows = [
        {
            "name": s.name,
            "duration_minutes": s.duration_minutes or "",
            "price_cents": s.price_cents or 0,
            "is_active": "yes" if s.is_active else "no",
        }
        for s in catalog_svc.list_services(db, tenant_id=actor.user.tenant_id)
    ]
    return _csv_response(
        rows,
        ["name", "duration_minutes", "price_cents", "is_active"],
        "services.csv",
    )


@router.get("/customers/{customer_id}", response_class=HTMLResponse)
def customer_detail_page(
    request: Request,
    customer_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    try:
        ctx = _customer_detail_ctx(request, actor, db, customer_id)
    except HTTPException:
        return HTMLResponse(
            "<h1>找不到顧客</h1>", status_code=status.HTTP_404_NOT_FOUND
        )
    return templates.TemplateResponse("customer_detail.html", ctx)


@router.post("/customers/tags", response_class=HTMLResponse)
def customer_create_tag(
    request: Request,
    name: str = Form(..., max_length=64),
    color: str = Form("", max_length=16),
    customer_id: int | None = Form(None),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """建立標籤——支援兩種來源表單：

    * 顧客 detail 頁（帶 customer_id）→ 回同一 detail partial。
    * 標籤管理檢視（_customers.html，無 customer_id）→ 回管理 partial。

    註：本路由須宣告於 /customers/{customer_id} 之前，否則 "tags" 會被
    當成 customer_id（比照 routers/customers.py 的順序註記）。
    """
    tid = actor.user.tenant_id
    error = None
    try:
        segments_svc.create_tag(
            db, tenant_id=tid, name=name.strip(), color=color.strip() or None
        )
    except HTTPException as exc:
        db.rollback()
        error = str(exc.detail)
    if customer_id is None:
        return templates.TemplateResponse(
            "_customers.html",
            _customers_admin_ctx(request, actor, db, error=error),
        )
    try:
        ctx = _customer_detail_ctx(request, actor, db, customer_id, error=error)
    except HTTPException:
        return HTMLResponse(
            "<h1>找不到顧客</h1>", status_code=status.HTTP_404_NOT_FOUND
        )
    return templates.TemplateResponse("_customer_detail.html", ctx)


@router.post("/customers/{customer_id}", response_class=HTMLResponse)
def customer_update(
    request: Request,
    customer_id: int,
    phone: str = Form(""),
    note: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        customers_svc.update_customer(
            db,
            tenant_id=tid,
            customer_id=customer_id,
            phone=phone.strip()[:32],
            note=note.strip()[:2048],
        )
    except HTTPException:
        return HTMLResponse(
            "<h1>找不到顧客</h1>", status_code=status.HTTP_404_NOT_FOUND
        )
    return templates.TemplateResponse(
        "_customer_detail.html",
        _customer_detail_ctx(
            request,
            actor,
            db,
            customer_id,
            error=error,
            saved="基本資料已更新",
        ),
    )


@router.post("/customers/{customer_id}/tags", response_class=HTMLResponse)
async def customer_sync_tags(
    request: Request,
    customer_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """整批同步顧客標籤：checkbox 勾選集合 vs 現況做 attach/detach。"""
    tid = actor.user.tenant_id
    form = await request.form()
    selected = {int(v) for v in form.getlist("tag_ids") if str(v).isdigit()}
    try:
        current = {
            t.id
            for t in segments_svc.list_tags_for_customer(
                db, tenant_id=tid, customer_id=customer_id
            )
        }
        for tag_id in selected - current:
            segments_svc.attach_tag(
                db, tenant_id=tid, customer_id=customer_id, tag_id=tag_id
            )
        for tag_id in current - selected:
            segments_svc.detach_tag(
                db, tenant_id=tid, customer_id=customer_id, tag_id=tag_id
            )
    except HTTPException:
        return HTMLResponse(
            "<h1>找不到顧客</h1>", status_code=status.HTTP_404_NOT_FOUND
        )
    return templates.TemplateResponse(
        "_customer_detail.html",
        _customer_detail_ctx(request, actor, db, customer_id, saved="標籤已更新"),
    )


@router.post("/customers/{customer_id}/points", response_class=HTMLResponse)
def customer_adjust_points(
    request: Request,
    customer_id: int,
    delta: int = Form(...),
    reason: str = Form("manual", max_length=64),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """店家手動調整點數（正加負扣）；扣點不足回錯誤訊息。"""
    tid = actor.user.tenant_id
    error = None
    saved = None
    try:
        customer = customers_svc.get_customer(
            db, tenant_id=tid, customer_id=customer_id
        )
        if delta > 0:
            membership_svc.earn_points(
                db, tenant_id=tid, customer=customer, delta=delta, reason=reason
            )
            db.commit()
            saved = f"已加 {delta} 點"
        elif delta < 0:
            try:
                membership_svc.redeem_points(
                    db,
                    tenant_id=tid,
                    customer=customer,
                    amount=-delta,
                    reason=reason,
                )
                db.commit()
                saved = f"已扣 {-delta} 點"
            except membership_svc.InsufficientPoints:
                db.rollback()
                error = "點數不足，無法扣點"
    except HTTPException:
        return HTMLResponse(
            "<h1>找不到顧客</h1>", status_code=status.HTTP_404_NOT_FOUND
        )
    return templates.TemplateResponse(
        "_customer_detail.html",
        _customer_detail_ctx(request, actor, db, customer_id, error=error, saved=saved),
    )


@router.post("/customers/{customer_id}/packages", response_class=HTMLResponse)
def customer_issue_package(
    request: Request,
    customer_id: int,
    package_id: int = Form(...),
    issuance_key: str = Form(..., max_length=64),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    error = None
    saved = None
    if not _require_ui_feature(db, actor, features_svc.SERVICE_PACKAGES):
        error = "服務套票功能尚未開通。"
    else:
        try:
            owned = packages_svc.issue_package(
                db,
                tenant_id=actor.user.tenant_id,
                customer_id=customer_id,
                package_id=package_id,
                actor_user_id=actor.user.id,
                issuance_key=issuance_key,
            )
            audit_svc.record_from_actor(
                db,
                actor,
                action="packages.issue",
                target=f"customer_package:{owned.id}",
                detail={
                    "customer_id": customer_id,
                    "package_id": package_id,
                    "price_cents": owned.price_cents_snapshot,
                },
                request=request,
            )
            db.commit()
            saved = f"已發行「{owned.package_name_snapshot}」"
        except packages_svc.ServicePackageError as exc:
            db.rollback()
            error = str(exc)
    return templates.TemplateResponse(
        "_customer_detail.html",
        _customer_detail_ctx(request, actor, db, customer_id, error=error, saved=saved),
    )


@router.post(
    "/customers/{customer_id}/packages/{customer_package_id}/cancel",
    response_class=HTMLResponse,
)
def customer_cancel_package(
    request: Request,
    customer_id: int,
    customer_package_id: int,
    note: str = Form("", max_length=255),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    error = None
    saved = None
    try:
        owned = packages_svc.cancel_customer_package(
            db,
            tenant_id=actor.user.tenant_id,
            customer_id=customer_id,
            customer_package_id=customer_package_id,
            actor_user_id=actor.user.id,
            note=note,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="packages.customer.cancel",
            target=f"customer_package:{owned.id}",
            detail={"customer_id": customer_id, "note": note.strip()[:255]},
            request=request,
        )
        db.commit()
        saved = (
            f"已作廢「{owned.package_name_snapshot}」未用次數；款項請另行退款／對帳。"
        )
    except packages_svc.ServicePackageError as exc:
        db.rollback()
        error = str(exc)
    return templates.TemplateResponse(
        "_customer_detail.html",
        _customer_detail_ctx(request, actor, db, customer_id, error=error, saved=saved),
    )


