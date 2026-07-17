"""UI 子模組(P4 純搬移自 routers/ui.py):店家自助:顧客管理(CRM+標籤)+ 備註。"""
from __future__ import annotations


from fastapi import Depends, Form, Request
from fastapi.responses import (
    HTMLResponse,
)
from sqlalchemy.orm import Session

from saas_mvp.deps import (
    Actor,
    get_db,
    require_ui_user,
)
from saas_mvp.services import customers as customers_svc
from saas_mvp.services import notes as notes_svc
from saas_mvp.services import segments as segments_svc
from saas_mvp.services.tenants import tenant_query
from fastapi import HTTPException

from saas_mvp.routers.ui._shared import (
    router, templates, _ctx,
)

# ── 店家自助：顧客管理（CRM + 標籤） ─────────────────────────────────────────


def _customers_admin_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    from saas_mvp.models.customer_tag_link import CustomerTagLink

    tid = actor.user.tenant_id
    tags = segments_svc.list_tags(db, tenant_id=tid)
    tag_by_id = {t.id: t for t in tags}
    tags_by_customer: dict[int, list] = {}
    for link in tenant_query(db, CustomerTagLink, tid).all():
        tag = tag_by_id.get(link.tag_id)
        if tag is not None:
            tags_by_customer.setdefault(link.customer_id, []).append(tag)
    return _ctx(
        request,
        actor,
        customers=customers_svc.list_customers(db, tenant_id=tid),
        tags=tags,
        tags_by_customer=tags_by_customer,
        **extra,
    )


# 註：本區段（顧客管理/標籤 CRUD/inline 編輯/刪除）與後方「顧客 CRM」區段
# （列表/搜尋/分頁/detail/匯入匯出/點數）在 upstream 合併時整併：
# GET /customers 主頁由 CRM 區段提供；本區段保留 /customers/list（標籤管理
# 檢視）與 tag 編輯/刪除、顧客 inline 編輯/刪除。建立標籤統一由 CRM 區段的
# POST /customers/tags 處理（支援帶/不帶 customer_id 兩種來源表單）。


@router.get("/customers/list", response_class=HTMLResponse)
def customers_list_partial(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """顧客管理 partial（標籤 CRUD + inline 編輯檢視；編輯列「取消」的目標）。"""
    return templates.TemplateResponse(
        "_customers.html", _customers_admin_ctx(request, actor, db)
    )


@router.get("/customers/tags/{tag_id}/edit", response_class=HTMLResponse)
def customers_edit_tag_form(
    request: Request,
    tag_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "_customers.html",
        _customers_admin_ctx(request, actor, db, editing_tag_id=tag_id),
    )


@router.post("/customers/tags/{tag_id}/update", response_class=HTMLResponse)
def customers_update_tag(
    request: Request,
    tag_id: int,
    name: str = Form(...),
    color: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    editing_tag_id = None
    try:
        segments_svc.update_tag(
            db, tenant_id=tid, tag_id=tag_id, name=name, color=color or None
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_tag_id = tag_id
    return templates.TemplateResponse(
        "_customers.html",
        _customers_admin_ctx(
            request, actor, db, error=error, editing_tag_id=editing_tag_id
        ),
    )


@router.post("/customers/tags/{tag_id}/delete", response_class=HTMLResponse)
def customers_delete_tag(
    request: Request,
    tag_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        segments_svc.delete_tag(db, tenant_id=tid, tag_id=tag_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_customers.html", _customers_admin_ctx(request, actor, db, error=error)
    )


@router.get("/customers/{customer_id}/edit", response_class=HTMLResponse)
def customers_edit_form(
    request: Request,
    customer_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "_customers.html",
        _customers_admin_ctx(request, actor, db, editing_customer_id=customer_id),
    )


@router.post("/customers/{customer_id}/update", response_class=HTMLResponse)
def customers_update(
    request: Request,
    customer_id: int,
    phone: str = Form(""),
    note: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    editing_customer_id = None
    try:
        customers_svc.update_customer(
            db,
            tenant_id=tid,
            customer_id=customer_id,
            phone=phone,
            note=note,
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_customer_id = customer_id
    return templates.TemplateResponse(
        "_customers.html",
        _customers_admin_ctx(
            request, actor, db, error=error, editing_customer_id=editing_customer_id
        ),
    )


@router.post("/customers/{customer_id}/delete", response_class=HTMLResponse)
def customers_delete(
    request: Request,
    customer_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        customers_svc.delete_customer(db, tenant_id=tid, customer_id=customer_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_customers.html", _customers_admin_ctx(request, actor, db, error=error)
    )


@router.post("/customers/{customer_id}/tags/attach", response_class=HTMLResponse)
def customers_attach_tag(
    request: Request,
    customer_id: int,
    tag_id: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        if not tag_id.strip():
            error = "請先選擇標籤"
        else:
            segments_svc.attach_tag(
                db, tenant_id=tid, customer_id=customer_id, tag_id=int(tag_id)
            )
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "標籤格式錯誤"
    return templates.TemplateResponse(
        "_customers.html", _customers_admin_ctx(request, actor, db, error=error)
    )


@router.post(
    "/customers/{customer_id}/tags/{tag_id}/detach", response_class=HTMLResponse
)
def customers_detach_tag(
    request: Request,
    customer_id: int,
    tag_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    # detach 冪等（未掛載為 no-op），不需錯誤處理
    segments_svc.detach_tag(db, tenant_id=tid, customer_id=customer_id, tag_id=tag_id)
    return templates.TemplateResponse(
        "_customers.html", _customers_admin_ctx(request, actor, db)
    )


# ── 店家自助：備註 ────────────────────────────────────────────────────────────


def _notes_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    return _ctx(
        request,
        actor,
        notes=notes_svc.list_notes(db, tenant_id=tid),
        **extra,
    )


@router.get("/notes", response_class=HTMLResponse)
def notes_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse("notes.html", _notes_ctx(request, actor, db))


@router.get("/notes/list", response_class=HTMLResponse)
def notes_list_partial(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse("_notes.html", _notes_ctx(request, actor, db))


@router.post("/notes", response_class=HTMLResponse)
def notes_create(
    request: Request,
    title: str = Form(...),
    content: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        notes_svc.create_note(
            db,
            tenant_id=tid,
            owner_id=actor.user.id,
            title=title,
            content=content,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_notes.html", _notes_ctx(request, actor, db, error=error)
    )


@router.get("/notes/{note_id}/edit", response_class=HTMLResponse)
def notes_edit_form(
    request: Request,
    note_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "_notes.html", _notes_ctx(request, actor, db, editing_id=note_id)
    )


@router.post("/notes/{note_id}/update", response_class=HTMLResponse)
def notes_update(
    request: Request,
    note_id: int,
    title: str = Form(...),
    content: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    editing_id = None
    try:
        notes_svc.update_note(
            db,
            tenant_id=tid,
            note_id=note_id,
            title=title,
            content=content,
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_id = note_id
    return templates.TemplateResponse(
        "_notes.html",
        _notes_ctx(request, actor, db, error=error, editing_id=editing_id),
    )


@router.post("/notes/{note_id}/delete", response_class=HTMLResponse)
def notes_delete(
    request: Request,
    note_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        notes_svc.delete_note(db, tenant_id=tid, note_id=note_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_notes.html", _notes_ctx(request, actor, db, error=error)
    )


