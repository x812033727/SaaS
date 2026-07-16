"""UI 子模組(P4 純搬移自 routers/ui.py):店家自助:API 金鑰 + 自動回覆規則。"""
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
from saas_mvp.services import api_keys as api_keys_svc
from saas_mvp.services import auto_reply as auto_reply_svc
from saas_mvp.services import customers as customers_svc
from saas_mvp.services import flex_menu as flex_menu_svc
from fastapi import HTTPException

from saas_mvp.routers.ui._shared import (
    router, templates, _ctx, _line_config_or_none,
)
from saas_mvp.routers.ui.booking import _booking_ctx

# ── 店家自助：API 金鑰 ────────────────────────────────────────────────────────


def _api_keys_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    return _ctx(
        request,
        actor,
        api_keys=api_keys_svc.list_keys(db, tenant_id=tid),
        **extra,
    )


@router.get("/api-keys", response_class=HTMLResponse)
def api_keys_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "api_keys.html", _api_keys_ctx(request, actor, db)
    )


@router.post("/api-keys", response_class=HTMLResponse)
def api_keys_create(
    request: Request,
    name: str = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    created_plain_key = None
    created_name = None
    if not name.strip():
        error = "名稱不可為空"
    elif len(name) > 128:
        error = "名稱長度上限 128"
    else:
        _, created_plain_key = api_keys_svc.create_key(
            db, tenant_id=tid, user_id=actor.user.id, name=name.strip()
        )
        created_name = name.strip()
    # 明文 key 只出現在本次回應（created_plain_key），之後永遠無法再取得。
    return templates.TemplateResponse(
        "_api_keys.html",
        _api_keys_ctx(
            request,
            actor,
            db,
            error=error,
            created_plain_key=created_plain_key,
            created_name=created_name,
        ),
    )


@router.post("/api-keys/{key_id}/revoke", response_class=HTMLResponse)
def api_keys_revoke(
    request: Request,
    key_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        api_keys_svc.revoke_key(db, tenant_id=tid, key_id=key_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_api_keys.html", _api_keys_ctx(request, actor, db, error=error)
    )


# ── 店家自助：自動回覆規則 ────────────────────────────────────────────────────


def _auto_reply_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    cfg = _line_config_or_none(db, tid)
    return _ctx(
        request,
        actor,
        rules=auto_reply_svc.list_rules(db, tenant_id=tid),
        flex_menus=flex_menu_svc.list_menus(db, tenant_id=tid),
        bot_mode=(cfg or {}).get("bot_mode", "translation"),
        **extra,
    )


def _auto_reply_form_kwargs(
    keyword: str,
    match_type: str,
    reply_type: str,
    reply_text: str,
    flex_menu_id: str,
    priority: str,
) -> dict:
    """表單值 → service 參數（空字串正規化為 None；驗證交給 service）。"""
    return {
        "keyword": keyword,
        "match_type": match_type,
        "reply_type": reply_type,
        "reply_text": reply_text.strip() or None,
        "flex_menu_id": int(flex_menu_id) if flex_menu_id.strip() else None,
        "priority": int(priority) if priority.strip() else 0,
    }


@router.get("/auto-reply", response_class=HTMLResponse)
def auto_reply_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "auto_reply.html", _auto_reply_ctx(request, actor, db)
    )


@router.get("/auto-reply/list", response_class=HTMLResponse)
def auto_reply_list_partial(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "_auto_reply.html", _auto_reply_ctx(request, actor, db)
    )


@router.post("/auto-reply", response_class=HTMLResponse)
def auto_reply_create(
    request: Request,
    keyword: str = Form(...),
    match_type: str = Form("contains"),
    reply_type: str = Form("text"),
    reply_text: str = Form(""),
    flex_menu_id: str = Form(""),
    priority: str = Form("0"),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        auto_reply_svc.create_rule(
            db,
            tenant_id=tid,
            **_auto_reply_form_kwargs(
                keyword, match_type, reply_type, reply_text, flex_menu_id, priority
            ),
        )
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "數字欄位格式錯誤"
    return templates.TemplateResponse(
        "_auto_reply.html", _auto_reply_ctx(request, actor, db, error=error)
    )


@router.get("/auto-reply/{rule_id}/edit", response_class=HTMLResponse)
def auto_reply_edit_form(
    request: Request,
    rule_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "_auto_reply.html",
        _auto_reply_ctx(request, actor, db, editing_rule_id=rule_id),
    )


@router.post("/auto-reply/{rule_id}/update", response_class=HTMLResponse)
def auto_reply_update(
    request: Request,
    rule_id: int,
    keyword: str = Form(...),
    match_type: str = Form("contains"),
    reply_type: str = Form("text"),
    reply_text: str = Form(""),
    flex_menu_id: str = Form(""),
    priority: str = Form("0"),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    editing_rule_id = None
    try:
        auto_reply_svc.update_rule(
            db,
            tenant_id=tid,
            rule_id=rule_id,
            **_auto_reply_form_kwargs(
                keyword, match_type, reply_type, reply_text, flex_menu_id, priority
            ),
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_rule_id = rule_id
    except ValueError:
        error = "數字欄位格式錯誤"
        editing_rule_id = rule_id
    return templates.TemplateResponse(
        "_auto_reply.html",
        _auto_reply_ctx(
            request, actor, db, error=error, editing_rule_id=editing_rule_id
        ),
    )


@router.post("/auto-reply/{rule_id}/toggle", response_class=HTMLResponse)
def auto_reply_toggle(
    request: Request,
    rule_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        rule = auto_reply_svc.get_rule(db, tenant_id=tid, rule_id=rule_id)
        auto_reply_svc.update_rule(
            db, tenant_id=tid, rule_id=rule_id, is_active=not rule.is_active
        )
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_auto_reply.html", _auto_reply_ctx(request, actor, db, error=error)
    )


@router.post("/auto-reply/{rule_id}/delete", response_class=HTMLResponse)
def auto_reply_delete(
    request: Request,
    rule_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        auto_reply_svc.delete_rule(db, tenant_id=tid, rule_id=rule_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_auto_reply.html", _auto_reply_ctx(request, actor, db, error=error)
    )


@router.post("/booking/customers/{customer_id}/blacklist", response_class=HTMLResponse)
def booking_set_blacklist(
    request: Request,
    customer_id: int,
    blacklisted: str = Form(...),
    reason: str = Form(default=""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """加入/解除顧客黑名單（硬性阻擋線上預約），重新渲染顧客卡片。"""
    tid = actor.user.tenant_id
    try:
        customers_svc.set_blacklist(
            db,
            tenant_id=tid,
            customer_id=customer_id,
            blacklisted=(blacklisted == "true"),
            reason=(reason.strip() or None),
        )
    except HTTPException:
        pass  # 查無顧客（跨租戶/已刪）時靜默，照常回渲染目前清單
    return templates.TemplateResponse(
        "_booking_customers.html", _booking_ctx(request, actor, db)
    )


