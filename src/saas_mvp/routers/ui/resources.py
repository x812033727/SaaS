"""UI 子模組(P4 純搬移自 routers/ui.py):店家自助:房間/設備資源 + 顧客諮詢表/同意書。"""
from __future__ import annotations

import datetime
import json

from fastapi import Depends, Form, Request
from fastapi.responses import (
    HTMLResponse,
)
from sqlalchemy.orm import Session

from saas_mvp.deps import (
    Actor,
    get_db,
    require_ui_owner,
    require_ui_user,
)
from saas_mvp.services import features as features_svc
from saas_mvp.services import audit as audit_svc
from saas_mvp.services import locations as locations_svc
from saas_mvp.services import catalog as catalog_svc
from saas_mvp.services import client_forms as client_forms_svc
from saas_mvp.services import bookable_resources as resources_svc

from saas_mvp.routers.ui._shared import (
    router, templates, _ctx, _opt_int, _require_ui_feature,
)
from saas_mvp.routers.ui.booking import _parse_slot_start
from saas_mvp.routers.ui.commerce import _feature_locked

# ── 店家自助：房間／設備資源（BOOKABLE_RESOURCES） ───────────────────────────


def _resources_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    resource_types = resources_svc.list_types(db, tenant_id=tid)
    resources = resources_svc.list_resources(db, tenant_id=tid)
    services = catalog_svc.list_services(db, tenant_id=tid)
    locations = locations_svc.list_locations(db, tenant_id=tid)
    windows = resources_svc.list_availability(db, tenant_id=tid)
    blocks = resources_svc.list_blocks(db, tenant_id=tid)
    windows_by_resource: dict[int, list] = {}
    blocks_by_resource: dict[int, list] = {}
    for window in windows:
        windows_by_resource.setdefault(window.resource_id, []).append(window)
    for block in blocks:
        blocks_by_resource.setdefault(block.resource_id, []).append(block)
    return _ctx(
        request,
        actor,
        resource_types=resource_types,
        resources=resources,
        services=services,
        locations=locations,
        requirements=resources_svc.list_requirements(db, tenant_id=tid),
        windows_by_resource=windows_by_resource,
        blocks_by_resource=blocks_by_resource,
        upcoming_allocations=resources_svc.list_upcoming_allocations(
            db, tenant_id=tid
        ),
        type_names={row.id: row.name for row in resource_types},
        resource_names={row.id: row.name for row in resources},
        service_names={row.id: row.name for row in services},
        location_names={row.id: row.name for row in locations},
        weekday_names=("週一", "週二", "週三", "週四", "週五", "週六", "週日"),
        can_manage_resources=(
            (getattr(actor.user, "role", None) or "owner") == "owner"
            or actor.user.is_admin
        ),
        **extra,
    )


def _resources_response(
    request: Request, actor: Actor, db: Session, *, error: str | None = None
):
    return templates.TemplateResponse(
        "_resources.html", _resources_ctx(request, actor, db, error=error)
    )


def _resources_enabled(db: Session, actor: Actor):
    return _require_ui_feature(db, actor, features_svc.BOOKABLE_RESOURCES)


@router.get("/resources", response_class=HTMLResponse)
def resources_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _resources_enabled(db, actor):
        return _feature_locked(
            request, actor, features_svc.BOOKABLE_RESOURCES, "房間／設備資源"
        )
    return templates.TemplateResponse(
        "resources.html", _resources_ctx(request, actor, db)
    )


@router.post("/resources/types", response_class=HTMLResponse)
def resources_create_type(
    request: Request,
    name: str = Form(..., max_length=128),
    description: str = Form("", max_length=2000),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _resources_enabled(db, actor):
        return _feature_locked(
            request, actor, features_svc.BOOKABLE_RESOURCES, "房間／設備資源"
        )
    error = None
    try:
        row = resources_svc.create_type(
            db, tenant_id=actor.user.tenant_id, name=name, description=description
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="resources.type.create",
            target=f"resource_type:{row.id}",
            request=request,
        )
        db.commit()
    except resources_svc.BookableResourceError as exc:
        db.rollback()
        error = str(exc)
    return _resources_response(request, actor, db, error=error)


@router.post("/resources/types/{resource_type_id}/active", response_class=HTMLResponse)
def resources_type_active(
    request: Request,
    resource_type_id: int,
    active: str = Form(...),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _resources_enabled(db, actor):
        return _feature_locked(
            request, actor, features_svc.BOOKABLE_RESOURCES, "房間／設備資源"
        )
    error = None
    try:
        row = resources_svc.set_type_active(
            db,
            tenant_id=actor.user.tenant_id,
            resource_type_id=resource_type_id,
            active=active == "true",
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="resources.type.active",
            target=f"resource_type:{row.id}",
            detail={"active": row.is_active},
            request=request,
        )
        db.commit()
    except resources_svc.BookableResourceError as exc:
        db.rollback()
        error = str(exc)
    return _resources_response(request, actor, db, error=error)


@router.post("/resources", response_class=HTMLResponse)
def resources_create(
    request: Request,
    resource_type_id: int = Form(...),
    name: str = Form(..., max_length=128),
    description: str = Form("", max_length=2000),
    internal_code: str = Form("", max_length=64),
    capacity: int = Form(1),
    location_id: str = Form(""),
    available_from: str = Form(""),
    available_until: str = Form(""),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _resources_enabled(db, actor):
        return _feature_locked(
            request, actor, features_svc.BOOKABLE_RESOURCES, "房間／設備資源"
        )
    error = None
    try:
        row = resources_svc.create_resource(
            db,
            tenant_id=actor.user.tenant_id,
            resource_type_id=resource_type_id,
            name=name,
            description=description,
            internal_code=internal_code,
            capacity=capacity,
            location_id=_opt_int(location_id),
            available_from=(
                datetime.date.fromisoformat(available_from) if available_from else None
            ),
            available_until=(
                datetime.date.fromisoformat(available_until)
                if available_until
                else None
            ),
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="resources.create",
            target=f"resource:{row.id}",
            request=request,
        )
        db.commit()
    except (resources_svc.BookableResourceError, ValueError) as exc:
        db.rollback()
        error = str(exc) or "日期格式不正確。"
    return _resources_response(request, actor, db, error=error)


@router.post("/resources/{resource_id}", response_class=HTMLResponse)
def resources_update(
    request: Request,
    resource_id: int,
    name: str = Form(..., max_length=128),
    description: str = Form("", max_length=2000),
    internal_code: str = Form("", max_length=64),
    capacity: int = Form(1),
    location_id: str = Form(""),
    available_from: str = Form(""),
    available_until: str = Form(""),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _resources_enabled(db, actor):
        return _feature_locked(
            request, actor, features_svc.BOOKABLE_RESOURCES, "房間／設備資源"
        )
    error = None
    try:
        row = resources_svc.update_resource(
            db,
            tenant_id=actor.user.tenant_id,
            resource_id=resource_id,
            name=name,
            description=description,
            internal_code=internal_code,
            capacity=capacity,
            location_id=_opt_int(location_id),
            available_from=(
                datetime.date.fromisoformat(available_from) if available_from else None
            ),
            available_until=(
                datetime.date.fromisoformat(available_until)
                if available_until
                else None
            ),
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="resources.update",
            target=f"resource:{row.id}",
            request=request,
        )
        db.commit()
    except (resources_svc.BookableResourceError, ValueError) as exc:
        db.rollback()
        error = str(exc) or "日期格式不正確。"
    return _resources_response(request, actor, db, error=error)


@router.post("/resources/{resource_id}/active", response_class=HTMLResponse)
def resources_active(
    request: Request,
    resource_id: int,
    active: str = Form(...),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _resources_enabled(db, actor):
        return _feature_locked(
            request, actor, features_svc.BOOKABLE_RESOURCES, "房間／設備資源"
        )
    error = None
    try:
        row = resources_svc.set_resource_active(
            db,
            tenant_id=actor.user.tenant_id,
            resource_id=resource_id,
            active=active == "true",
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="resources.active",
            target=f"resource:{row.id}",
            detail={"active": row.is_active},
            request=request,
        )
        db.commit()
    except resources_svc.BookableResourceError as exc:
        db.rollback()
        error = str(exc)
    return _resources_response(request, actor, db, error=error)


@router.post("/resource-requirements", response_class=HTMLResponse)
def resources_set_requirement(
    request: Request,
    service_id: int = Form(...),
    resource_type_id: int = Form(...),
    quantity: int = Form(1),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _resources_enabled(db, actor):
        return _feature_locked(
            request, actor, features_svc.BOOKABLE_RESOURCES, "房間／設備資源"
        )
    error = None
    try:
        row = resources_svc.set_requirement(
            db,
            tenant_id=actor.user.tenant_id,
            service_id=service_id,
            resource_type_id=resource_type_id,
            quantity=quantity,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="resources.requirement.set",
            target=f"requirement:{row.id}",
            request=request,
        )
        db.commit()
    except resources_svc.BookableResourceError as exc:
        db.rollback()
        error = str(exc)
    return _resources_response(request, actor, db, error=error)


@router.post("/resource-requirements/{requirement_id}/delete", response_class=HTMLResponse)
def resources_remove_requirement(
    request: Request,
    requirement_id: int,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _resources_enabled(db, actor):
        return _feature_locked(
            request, actor, features_svc.BOOKABLE_RESOURCES, "房間／設備資源"
        )
    error = None
    try:
        resources_svc.remove_requirement(
            db, tenant_id=actor.user.tenant_id, requirement_id=requirement_id
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="resources.requirement.delete",
            target=f"requirement:{requirement_id}",
            request=request,
        )
        db.commit()
    except resources_svc.BookableResourceError as exc:
        db.rollback()
        error = str(exc)
    return _resources_response(request, actor, db, error=error)


@router.post("/resources/{resource_id}/availability", response_class=HTMLResponse)
def resources_add_availability(
    request: Request,
    resource_id: int,
    weekday: int = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _resources_enabled(db, actor):
        return _feature_locked(
            request, actor, features_svc.BOOKABLE_RESOURCES, "房間／設備資源"
        )
    error = None
    try:
        resources_svc.add_availability(
            db,
            tenant_id=actor.user.tenant_id,
            resource_id=resource_id,
            weekday=weekday,
            start_time=datetime.time.fromisoformat(start_time),
            end_time=datetime.time.fromisoformat(end_time),
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="resources.availability.add",
            target=f"resource:{resource_id}",
            request=request,
        )
        db.commit()
    except (resources_svc.BookableResourceError, ValueError) as exc:
        db.rollback()
        error = str(exc) or "時間格式不正確。"
    return _resources_response(request, actor, db, error=error)


@router.post("/resources/availability/{availability_id}/delete", response_class=HTMLResponse)
def resources_remove_availability(
    request: Request,
    availability_id: int,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _resources_enabled(db, actor):
        return _feature_locked(
            request, actor, features_svc.BOOKABLE_RESOURCES, "房間／設備資源"
        )
    error = None
    try:
        resources_svc.remove_availability(
            db, tenant_id=actor.user.tenant_id, availability_id=availability_id
        )
        db.commit()
    except resources_svc.BookableResourceError as exc:
        db.rollback()
        error = str(exc)
    return _resources_response(request, actor, db, error=error)


@router.post("/resources/{resource_id}/blocks", response_class=HTMLResponse)
def resources_add_block(
    request: Request,
    resource_id: int,
    starts_at: str = Form(...),
    ends_at: str = Form(...),
    reason: str = Form("", max_length=255),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _resources_enabled(db, actor):
        return _feature_locked(
            request, actor, features_svc.BOOKABLE_RESOURCES, "房間／設備資源"
        )
    error = None
    try:
        resources_svc.add_block(
            db,
            tenant_id=actor.user.tenant_id,
            resource_id=resource_id,
            starts_at=_parse_slot_start(starts_at),
            ends_at=_parse_slot_start(ends_at),
            reason=reason,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="resources.block.add",
            target=f"resource:{resource_id}",
            request=request,
        )
        db.commit()
    except (resources_svc.BookableResourceError, ValueError) as exc:
        db.rollback()
        error = str(exc) or "日期時間格式不正確。"
    return _resources_response(request, actor, db, error=error)


@router.post("/resources/blocks/{block_id}/delete", response_class=HTMLResponse)
def resources_remove_block(
    request: Request,
    block_id: int,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _resources_enabled(db, actor):
        return _feature_locked(
            request, actor, features_svc.BOOKABLE_RESOURCES, "房間／設備資源"
        )
    error = None
    try:
        resources_svc.remove_block(
            db, tenant_id=actor.user.tenant_id, block_id=block_id
        )
        db.commit()
    except resources_svc.BookableResourceError as exc:
        db.rollback()
        error = str(exc)
    return _resources_response(request, actor, db, error=error)


# ── 店家自助：顧客諮詢表／同意書（CLIENT_FORMS） ──────────────────────────────


def _client_forms_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    rows = client_forms_svc.list_templates(db, tenant_id=tid)
    question_map = {
        row.id: client_forms_svc.questions(db, tenant_id=tid, template_id=row.id)
        for row in rows
    }
    option_map = {
        question.id: (
            json.loads(question.options_json) if question.options_json else []
        )
        for form_questions in question_map.values()
        for question in form_questions
    }
    services = catalog_svc.list_services(db, tenant_id=tid)
    return _ctx(
        request,
        actor,
        form_templates=rows,
        questions_by_template=question_map,
        question_options=option_map,
        services=services,
        service_names={service.id: service.name for service in services},
        **extra,
    )


@router.get("/client-forms", response_class=HTMLResponse)
def client_forms_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.CLIENT_FORMS):
        return _feature_locked(
            request, actor, features_svc.CLIENT_FORMS, "顧客表單／同意書"
        )
    return templates.TemplateResponse(
        "client_forms.html", _client_forms_ctx(request, actor, db)
    )


@router.post("/client-forms", response_class=HTMLResponse)
def client_forms_create(
    request: Request,
    name: str = Form(..., max_length=128),
    intro: str = Form("", max_length=4000),
    consent_text: str = Form(..., max_length=4000),
    service_id: str = Form(""),
    require_signature: str = Form(""),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    error = None
    if not _require_ui_feature(db, actor, features_svc.CLIENT_FORMS):
        return _feature_locked(
            request, actor, features_svc.CLIENT_FORMS, "顧客表單／同意書"
        )
    try:
        row = client_forms_svc.create_template(
            db,
            tenant_id=actor.user.tenant_id,
            name=name,
            intro=intro,
            consent_text=consent_text,
            service_id=_opt_int(service_id),
            require_signature=require_signature == "true",
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="client_forms.create",
            target=f"form:{row.id}",
            request=request,
        )
        db.commit()
    except client_forms_svc.ClientFormError as exc:
        db.rollback()
        error = str(exc)
    return templates.TemplateResponse(
        "_client_forms.html", _client_forms_ctx(request, actor, db, error=error)
    )


@router.post("/client-forms/{template_id}/questions", response_class=HTMLResponse)
def client_forms_add_question(
    request: Request,
    template_id: int,
    label: str = Form(..., max_length=255),
    field_type: str = Form(...),
    required: str = Form(""),
    options: str = Form("", max_length=6000),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    error = None
    if not _require_ui_feature(db, actor, features_svc.CLIENT_FORMS):
        return _feature_locked(
            request, actor, features_svc.CLIENT_FORMS, "顧客表單／同意書"
        )
    try:
        client_forms_svc.add_question(
            db,
            tenant_id=actor.user.tenant_id,
            template_id=template_id,
            label=label,
            field_type=field_type,
            required=required == "true",
            options=options,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="client_forms.question.add",
            target=f"form:{template_id}",
            request=request,
        )
        db.commit()
    except client_forms_svc.ClientFormError as exc:
        db.rollback()
        error = str(exc)
    return templates.TemplateResponse(
        "_client_forms.html", _client_forms_ctx(request, actor, db, error=error)
    )


@router.post("/client-forms/{template_id}/active", response_class=HTMLResponse)
def client_forms_active(
    request: Request,
    template_id: int,
    active: str = Form(...),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    error = None
    if not _require_ui_feature(db, actor, features_svc.CLIENT_FORMS):
        return _feature_locked(
            request, actor, features_svc.CLIENT_FORMS, "顧客表單／同意書"
        )
    try:
        row = client_forms_svc.set_active(
            db,
            tenant_id=actor.user.tenant_id,
            template_id=template_id,
            active=active == "true",
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="client_forms.active",
            target=f"form:{row.id}",
            detail={"active": row.is_active},
            request=request,
        )
        db.commit()
    except client_forms_svc.ClientFormError as exc:
        db.rollback()
        error = str(exc)
    return templates.TemplateResponse(
        "_client_forms.html", _client_forms_ctx(request, actor, db, error=error)
    )


