"""UI 子模組(P4 純搬移自 routers/ui.py):店家自助:分店 + 員工 + 服務項目。"""
from __future__ import annotations


from fastapi import Depends, Form, Request
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
from saas_mvp.services import features as features_svc
from saas_mvp.services import locations as locations_svc
from saas_mvp.services import staff as staff_svc
from saas_mvp.services import catalog as catalog_svc
from fastapi import HTTPException

from saas_mvp.routers.ui._shared import (
    router, templates, _ctx, _opt_int, _require_ui_feature,
)
from saas_mvp.routers.ui.booking import _parse_slot_start
from saas_mvp.routers.ui.commerce import _feature_locked

# ── 店家自助：分店（MULTI_LOCATION） ─────────────────────────────────────────────


def _locations_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    rows = locations_svc.list_locations(db, tenant_id=tid)
    active_count = sum(1 for location in rows if location.is_active)
    return _ctx(
        request,
        actor,
        locations=rows,
        active_count=active_count,
        max_locations=settings.max_locations_per_tenant,
        **extra,
    )


@router.get("/locations", response_class=HTMLResponse)
def locations_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.MULTI_LOCATION):
        return _feature_locked(request, actor, features_svc.MULTI_LOCATION, "多分店")
    return templates.TemplateResponse(
        "locations.html", _locations_ctx(request, actor, db)
    )


@router.post("/locations", response_class=HTMLResponse)
def locations_create(
    request: Request,
    name: str = Form(...),
    address: str = Form(""),
    phone: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.MULTI_LOCATION):
        return _feature_locked(request, actor, features_svc.MULTI_LOCATION, "多分店")
    tid = actor.user.tenant_id
    error = None
    try:
        locations_svc.create_location(
            db,
            tenant_id=tid,
            name=name,
            address=address or None,
            phone=phone or None,
        )
    except locations_svc.LocationLimitError as exc:
        error = str(exc)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_locations.html", _locations_ctx(request, actor, db, error=error)
    )


@router.post("/locations/{location_id}/update", response_class=HTMLResponse)
def locations_update(
    request: Request,
    location_id: int,
    name: str = Form(...),
    address: str = Form(""),
    phone: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.MULTI_LOCATION):
        return _feature_locked(request, actor, features_svc.MULTI_LOCATION, "多分店")
    tid = actor.user.tenant_id
    error = None
    try:
        locations_svc.update_location(
            db,
            tenant_id=tid,
            location_id=location_id,
            name=name,
            address=address or None,
            phone=phone or None,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_locations.html", _locations_ctx(request, actor, db, error=error)
    )


@router.post("/locations/{location_id}/deactivate", response_class=HTMLResponse)
def locations_deactivate(
    request: Request,
    location_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.MULTI_LOCATION):
        return _feature_locked(request, actor, features_svc.MULTI_LOCATION, "多分店")
    tid = actor.user.tenant_id
    error = None
    try:
        locations_svc.update_location(
            db, tenant_id=tid, location_id=location_id, is_active=False
        )
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_locations.html", _locations_ctx(request, actor, db, error=error)
    )


@router.post("/locations/{location_id}/activate", response_class=HTMLResponse)
def locations_activate(
    request: Request,
    location_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.MULTI_LOCATION):
        return _feature_locked(request, actor, features_svc.MULTI_LOCATION, "多分店")
    tid = actor.user.tenant_id
    error = None
    try:
        locations_svc.update_location(
            db, tenant_id=tid, location_id=location_id, is_active=True
        )
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_locations.html", _locations_ctx(request, actor, db, error=error)
    )


@router.post("/locations/{location_id}/delete", response_class=HTMLResponse)
def locations_delete(
    request: Request,
    location_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.MULTI_LOCATION):
        return _feature_locked(request, actor, features_svc.MULTI_LOCATION, "多分店")
    tid = actor.user.tenant_id
    error = None
    try:
        locations_svc.delete_location(db, tenant_id=tid, location_id=location_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_locations.html", _locations_ctx(request, actor, db, error=error)
    )


# ── 店家自助：員工（STAFF_SCHEDULING） ──────────────────────────────────────────


def _staff_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    rows = staff_svc.list_staff(db, tenant_id=tid)
    shifts = {
        s.id: staff_svc.list_shifts(db, tenant_id=tid, staff_id=s.id) for s in rows
    }
    leaves = {
        s.id: staff_svc.list_leaves(db, tenant_id=tid, staff_id=s.id) for s in rows
    }
    return _ctx(
        request,
        actor,
        staff_rows=rows,
        staff_shifts=shifts,
        staff_leaves=leaves,
        shift_templates=staff_svc.SHIFT_TEMPLATES,
        locations=locations_svc.list_locations(db, tenant_id=tid),
        **extra,
    )


@router.get("/staff", response_class=HTMLResponse)
def staff_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    return templates.TemplateResponse("staff.html", _staff_ctx(request, actor, db))


@router.get("/staff/list", response_class=HTMLResponse)
def staff_list_partial(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """員工列表 partial（班表/請假編輯列「取消」的 hx-get 目標）。"""
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    return templates.TemplateResponse(
        "_staff_list.html", _staff_ctx(request, actor, db)
    )


@router.post("/staff", response_class=HTMLResponse)
def staff_create(
    request: Request,
    name: str = Form(...),
    role: str = Form(""),
    location_id: str = Form(""),
    booking_mode: str = Form("capacity"),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        staff_svc.create_staff(
            db,
            tenant_id=tid,
            name=name,
            role=role or None,
            location_id=_opt_int(location_id),
            booking_mode=booking_mode,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "分店格式錯誤"
    return templates.TemplateResponse(
        "_staff_list.html", _staff_ctx(request, actor, db, error=error)
    )


@router.post("/staff/{staff_id}/update", response_class=HTMLResponse)
def staff_update(
    request: Request,
    staff_id: int,
    name: str = Form(...),
    role: str = Form(""),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        staff_svc.update_staff(
            db,
            tenant_id=tid,
            staff_id=staff_id,
            name=name,
            role=role or None,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_staff_list.html", _staff_ctx(request, actor, db, error=error)
    )


@router.post("/staff/{staff_id}/deactivate", response_class=HTMLResponse)
def staff_deactivate(
    request: Request,
    staff_id: int,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        staff_svc.update_staff(db, tenant_id=tid, staff_id=staff_id, is_active=False)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_staff_list.html", _staff_ctx(request, actor, db, error=error)
    )


@router.post("/staff/{staff_id}/activate", response_class=HTMLResponse)
def staff_activate(
    request: Request,
    staff_id: int,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        staff_svc.update_staff(db, tenant_id=tid, staff_id=staff_id, is_active=True)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_staff_list.html", _staff_ctx(request, actor, db, error=error)
    )


@router.post("/staff/{staff_id}/delete", response_class=HTMLResponse)
def staff_delete(
    request: Request,
    staff_id: int,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        staff_svc.delete_staff(db, tenant_id=tid, staff_id=staff_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_staff_list.html", _staff_ctx(request, actor, db, error=error)
    )


@router.post("/staff/{staff_id}/rotate-token", response_class=HTMLResponse)
def staff_rotate_token(
    request: Request,
    staff_id: int,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        staff_svc.rotate_token(db, tenant_id=tid, staff_id=staff_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_staff_list.html", _staff_ctx(request, actor, db, error=error)
    )


@router.post("/staff/{staff_id}/shifts", response_class=HTMLResponse)
def staff_create_shift(
    request: Request,
    staff_id: int,
    start_time: str = Form(...),
    end_time: str = Form(...),
    weekday: str = Form(""),
    rotation: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        staff_svc.create_shift(
            db,
            tenant_id=tid,
            staff_id=staff_id,
            start_time=start_time,
            end_time=end_time,
            weekday=_opt_int(weekday),
            rotation=rotation or None,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "星期格式錯誤"
    return templates.TemplateResponse(
        "_staff_list.html", _staff_ctx(request, actor, db, error=error)
    )


@router.post("/staff/{staff_id}/shifts/bulk", response_class=HTMLResponse)
def staff_bulk_shifts(
    request: Request,
    staff_id: int,
    template: str = Form(...),
    weekdays: list[str] = Form(default=[]),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """以內建模板批量排班（對標 vibeaico「內建模板一鍵套用」）。"""
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    tid = actor.user.tenant_id
    error = None
    saved = None
    try:
        wd = [int(w) for w in weekdays if w != ""]
        result = staff_svc.bulk_create_shifts_from_template(
            db,
            tenant_id=tid,
            staff_id=staff_id,
            template=template,
            weekdays=wd,
        )
        saved = f"已套用模板：新增 {result['created']} 筆、略過 {result['skipped']} 筆（已存在）。"
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "星期格式錯誤"
    return templates.TemplateResponse(
        "_staff_list.html", _staff_ctx(request, actor, db, error=error, bulk_msg=saved)
    )


@router.get("/staff/{staff_id}/shifts/{shift_id}/edit", response_class=HTMLResponse)
def staff_edit_shift_form(
    request: Request,
    staff_id: int,
    shift_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    return templates.TemplateResponse(
        "_staff_list.html",
        _staff_ctx(request, actor, db, editing_shift_id=shift_id),
    )


@router.post("/staff/{staff_id}/shifts/{shift_id}/update", response_class=HTMLResponse)
def staff_update_shift(
    request: Request,
    staff_id: int,
    shift_id: int,
    start_time: str = Form(...),
    end_time: str = Form(...),
    weekday: str = Form(""),
    rotation: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    tid = actor.user.tenant_id
    error = None
    editing_shift_id = None
    try:
        # weekday 一律帶明確值（int 或 None=每日）——表單的 select 永遠有值。
        staff_svc.update_shift(
            db,
            tenant_id=tid,
            staff_id=staff_id,
            shift_id=shift_id,
            start_time=start_time,
            end_time=end_time,
            weekday=_opt_int(weekday),
            rotation=rotation or None,
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_shift_id = shift_id
    except ValueError:
        error = "星期格式錯誤"
        editing_shift_id = shift_id
    return templates.TemplateResponse(
        "_staff_list.html",
        _staff_ctx(request, actor, db, error=error, editing_shift_id=editing_shift_id),
    )


@router.post("/staff/{staff_id}/shifts/{shift_id}/delete", response_class=HTMLResponse)
def staff_delete_shift(
    request: Request,
    staff_id: int,
    shift_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        staff_svc.delete_shift(db, tenant_id=tid, staff_id=staff_id, shift_id=shift_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_staff_list.html", _staff_ctx(request, actor, db, error=error)
    )


@router.post("/staff/{staff_id}/leaves", response_class=HTMLResponse)
def staff_create_leave(
    request: Request,
    staff_id: int,
    start_at: str = Form(...),
    end_at: str = Form(...),
    reason: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        staff_svc.create_leave(
            db,
            tenant_id=tid,
            staff_id=staff_id,
            start_at=_parse_slot_start(start_at),
            end_at=_parse_slot_start(end_at),
            reason=reason or None,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "請假時間格式錯誤"
    return templates.TemplateResponse(
        "_staff_list.html", _staff_ctx(request, actor, db, error=error)
    )


@router.get("/staff/{staff_id}/leaves/{leave_id}/edit", response_class=HTMLResponse)
def staff_edit_leave_form(
    request: Request,
    staff_id: int,
    leave_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    return templates.TemplateResponse(
        "_staff_list.html",
        _staff_ctx(request, actor, db, editing_leave_id=leave_id),
    )


@router.post("/staff/{staff_id}/leaves/{leave_id}/update", response_class=HTMLResponse)
def staff_update_leave(
    request: Request,
    staff_id: int,
    leave_id: int,
    start_at: str = Form(...),
    end_at: str = Form(...),
    reason: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    tid = actor.user.tenant_id
    error = None
    editing_leave_id = None
    try:
        staff_svc.update_leave(
            db,
            tenant_id=tid,
            staff_id=staff_id,
            leave_id=leave_id,
            start_at=_parse_slot_start(start_at),
            end_at=_parse_slot_start(end_at),
            reason=reason or None,
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_leave_id = leave_id
    except ValueError:
        error = "請假時間格式錯誤"
        editing_leave_id = leave_id
    return templates.TemplateResponse(
        "_staff_list.html",
        _staff_ctx(request, actor, db, error=error, editing_leave_id=editing_leave_id),
    )


@router.post("/staff/{staff_id}/leaves/{leave_id}/delete", response_class=HTMLResponse)
def staff_delete_leave(
    request: Request,
    staff_id: int,
    leave_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        staff_svc.delete_leave(db, tenant_id=tid, staff_id=staff_id, leave_id=leave_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_staff_list.html", _staff_ctx(request, actor, db, error=error)
    )


# ── 店家自助：服務項目（SERVICE_CATALOG） ───────────────────────────────────────


def _services_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    services = catalog_svc.list_services(db, tenant_id=tid)
    staff_rows = staff_svc.list_staff(db, tenant_id=tid)
    staff_by_id = {s.id: s for s in staff_rows}
    svc_staff: dict[int, list] = {}
    for svc in services:
        links = catalog_svc.list_service_staff(db, tenant_id=tid, service_id=svc.id)
        svc_staff[svc.id] = [
            staff_by_id[ln.staff_id] for ln in links if ln.staff_id in staff_by_id
        ]
    return _ctx(
        request,
        actor,
        categories=catalog_svc.list_categories(db, tenant_id=tid),
        services=services,
        staff_rows=staff_rows,
        service_staff=svc_staff,
        locations=locations_svc.list_locations(db, tenant_id=tid),
        **extra,
    )


@router.get("/services", response_class=HTMLResponse)
def services_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.SERVICE_CATALOG):
        return _feature_locked(request, actor, features_svc.SERVICE_CATALOG, "服務項目")
    return templates.TemplateResponse(
        "services.html", _services_ctx(request, actor, db)
    )


@router.post("/services/categories", response_class=HTMLResponse)
def services_create_category(
    request: Request,
    name: str = Form(...),
    sort_order: int = Form(0),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.SERVICE_CATALOG):
        return _feature_locked(request, actor, features_svc.SERVICE_CATALOG, "服務項目")
    tid = actor.user.tenant_id
    error = None
    try:
        catalog_svc.create_category(db, tenant_id=tid, name=name, sort_order=sort_order)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_services_list.html", _services_ctx(request, actor, db, error=error)
    )


@router.post("/services/categories/{category_id}/edit", response_class=HTMLResponse)
def services_update_category(
    request: Request,
    category_id: int,
    name: str = Form(...),
    sort_order: int = Form(0),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.SERVICE_CATALOG):
        return _feature_locked(request, actor, features_svc.SERVICE_CATALOG, "服務項目")
    tid = actor.user.tenant_id
    error = None
    try:
        catalog_svc.update_category(
            db,
            tenant_id=tid,
            category_id=category_id,
            name=name,
            sort_order=sort_order,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_services_list.html", _services_ctx(request, actor, db, error=error)
    )


@router.post("/services/categories/{category_id}/delete", response_class=HTMLResponse)
def services_delete_category(
    request: Request,
    category_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.SERVICE_CATALOG):
        return _feature_locked(request, actor, features_svc.SERVICE_CATALOG, "服務項目")
    tid = actor.user.tenant_id
    error = None
    try:
        catalog_svc.delete_category(db, tenant_id=tid, category_id=category_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_services_list.html", _services_ctx(request, actor, db, error=error)
    )


@router.post("/services", response_class=HTMLResponse)
def services_create(
    request: Request,
    name: str = Form(...),
    duration_minutes: int = Form(60),
    price_cents: int = Form(0),
    category_id: str = Form(""),
    location_id: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.SERVICE_CATALOG):
        return _feature_locked(request, actor, features_svc.SERVICE_CATALOG, "服務項目")
    tid = actor.user.tenant_id
    error = None
    try:
        catalog_svc.create_service(
            db,
            tenant_id=tid,
            name=name,
            duration_minutes=duration_minutes,
            price_cents=price_cents,
            category_id=_opt_int(category_id),
            location_id=_opt_int(location_id),
        )
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "分類或分店格式錯誤"
    return templates.TemplateResponse(
        "_services_list.html", _services_ctx(request, actor, db, error=error)
    )


@router.post("/services/{service_id}/edit", response_class=HTMLResponse)
def services_update(
    request: Request,
    service_id: int,
    name: str = Form(...),
    duration_minutes: int = Form(60),
    price_cents: int = Form(0),
    category_id: str = Form(""),
    location_id: str = Form(""),
    is_active: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.SERVICE_CATALOG):
        return _feature_locked(request, actor, features_svc.SERVICE_CATALOG, "服務項目")
    tid = actor.user.tenant_id
    error = None
    try:
        catalog_svc.update_service(
            db,
            tenant_id=tid,
            service_id=service_id,
            name=name,
            duration_minutes=duration_minutes,
            price_cents=price_cents,
            category_id=_opt_int(category_id),
            location_id=_opt_int(location_id),
            is_active=(is_active == "on"),
        )
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "分類或分店格式錯誤"
    return templates.TemplateResponse(
        "_services_list.html", _services_ctx(request, actor, db, error=error)
    )


@router.post("/services/{service_id}/delete", response_class=HTMLResponse)
def services_delete(
    request: Request,
    service_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.SERVICE_CATALOG):
        return _feature_locked(request, actor, features_svc.SERVICE_CATALOG, "服務項目")
    tid = actor.user.tenant_id
    error = None
    try:
        catalog_svc.delete_service(db, tenant_id=tid, service_id=service_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_services_list.html", _services_ctx(request, actor, db, error=error)
    )


@router.post("/services/{service_id}/staff", response_class=HTMLResponse)
def services_assign_staff(
    request: Request,
    service_id: int,
    staff_id: int = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.SERVICE_CATALOG):
        return _feature_locked(request, actor, features_svc.SERVICE_CATALOG, "服務項目")
    tid = actor.user.tenant_id
    error = None
    try:
        catalog_svc.assign_staff(
            db, tenant_id=tid, service_id=service_id, staff_id=staff_id
        )
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_services_list.html", _services_ctx(request, actor, db, error=error)
    )


@router.post(
    "/services/{service_id}/staff/{staff_id}/unassign", response_class=HTMLResponse
)
def services_unassign_staff(
    request: Request,
    service_id: int,
    staff_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.SERVICE_CATALOG):
        return _feature_locked(request, actor, features_svc.SERVICE_CATALOG, "服務項目")
    tid = actor.user.tenant_id
    error = None
    try:
        catalog_svc.unassign_staff(
            db, tenant_id=tid, service_id=service_id, staff_id=staff_id
        )
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_services_list.html", _services_ctx(request, actor, db, error=error)
    )


