"""UI 子模組(P4 純搬移自 routers/ui.py):店家自助:通知歷程 + 行銷活動 + 圖文選單卡片 + 作品集 + 公開店家頁。"""
from __future__ import annotations

import datetime

from fastapi import Depends, Form, Query, Request
from fastapi.responses import (
    HTMLResponse,
)
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.deps import (
    Actor,
    get_db,
    require_ui_user,
)
from saas_mvp.line_client import (
    LinePushClient,
    get_push_client,
)
from saas_mvp.services import features as features_svc
from saas_mvp.services import locations as locations_svc
from saas_mvp.services import marketing as marketing_svc
from saas_mvp.services import flex_menu as flex_menu_svc
from saas_mvp.services import portfolio as portfolio_svc
from saas_mvp.services import profile as profile_svc
from saas_mvp.services import segments as segments_svc
from saas_mvp.services import notifications_history as notif_history_svc
from saas_mvp.services import push_quota as push_quota_svc
from saas_mvp.models.campaign import Campaign
from saas_mvp.services.tenants import tenant_query
from fastapi import HTTPException

from saas_mvp.routers.ui._shared import (
    router, templates, _ctx, _is_htmx, _opt_int, _require_ui_feature,
)
from saas_mvp.routers.ui.booking import _parse_slot_start
from saas_mvp.routers.ui.commerce import _feature_locked

# ── 店家自助：通知與推播歷程（唯讀） ───────────────────────────────────────────

_NOTIF_PAGE_SIZE = 50
_NOTIF_TABS = ("booking", "campaign", "usage")


def _notifications_ctx(
    request: Request,
    actor: Actor,
    db: Session,
    *,
    tab: str = "booking",
    status_filter: str = "",
    page: int = 1,
    **extra,
) -> dict:
    tid = actor.user.tenant_id
    if tab not in _NOTIF_TABS:
        tab = "booking"
    page = max(1, page)
    offset = (page - 1) * _NOTIF_PAGE_SIZE

    rows: list = []
    total = 0
    campaign_names: dict[int, str] = {}
    usage_history: list[dict] = []
    push_status: dict | None = None

    if tab == "booking":
        rows, total = notif_history_svc.list_booking_notifications(
            db,
            tenant_id=tid,
            status=status_filter or None,
            limit=_NOTIF_PAGE_SIZE,
            offset=offset,
        )
    elif tab == "campaign":
        rows, total = notif_history_svc.list_campaign_sends(
            db,
            tenant_id=tid,
            status=status_filter or None,
            limit=_NOTIF_PAGE_SIZE,
            offset=offset,
        )
        campaign_ids = {r.campaign_id for r in rows}
        if campaign_ids:
            campaign_names = {
                c.id: c.name
                for c in tenant_query(db, Campaign, tid)
                .filter(Campaign.id.in_(campaign_ids))
                .all()
            }
    else:  # usage
        usage_history = notif_history_svc.push_usage_history(db, tenant_id=tid)
        push_status = push_quota_svc.get_push_quota_status(db, tid)

    pages = max(1, -(-total // _NOTIF_PAGE_SIZE))  # ceil
    return _ctx(
        request,
        actor,
        tab=tab,
        status_filter=status_filter,
        rows=rows,
        total=total,
        page=min(page, pages),
        pages=pages,
        campaign_names=campaign_names,
        usage_history=usage_history,
        push_status=push_status,
        **extra,
    )


@router.get("/notifications", response_class=HTMLResponse)
def notifications_page(
    request: Request,
    tab: str = Query(default="booking"),
    status_filter: str = Query(default="", alias="status", max_length=16),
    page: int = Query(default=1, ge=1),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    ctx = _notifications_ctx(
        request, actor, db, tab=tab, status_filter=status_filter, page=page
    )
    if _is_htmx(request):
        return templates.TemplateResponse("_notifications_list.html", ctx)
    return templates.TemplateResponse("notifications.html", ctx)


# ── 店家自助：行銷活動（MARKETING_AUTO） ────────────────────────────────────────


def _describe_segment(segment_json: str | None, tag_names: dict[int, str]) -> list[str]:
    """把 segment_json 反解成人話 chips（列表頁顯示）。malformed 回原字串。"""
    import json as _json

    if not segment_json:
        return []
    try:
        filters = _json.loads(segment_json)
        if not isinstance(filters, dict):
            return [str(segment_json)]
    except ValueError:
        return [str(segment_json)]
    chips: list[str] = []
    if filters.get("tag_ids"):
        names = [
            tag_names.get(t, f"標籤#{t}")
            for t in filters["tag_ids"]
            if isinstance(t, int) or str(t).isdigit()
        ]
        if names:
            chips.append("標籤：" + "、".join(str(n) for n in names))
    if filters.get("tier"):
        chips.append(f"等級：{filters['tier']}")
    if filters.get("min_bookings") is not None:
        chips.append(f"預約 ≥ {filters['min_bookings']} 次")
    if filters.get("location_id") is not None:
        chips.append(f"分店 #{filters['location_id']}")
    return chips


def _campaigns_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    rows = tenant_query(db, Campaign, tid).order_by(Campaign.id.desc()).all()
    tags = segments_svc.list_tags(db, tenant_id=tid)
    tag_names = {t.id: t.name for t in tags}
    locations = locations_svc.list_locations(db, tenant_id=tid)
    segment_chips = {c.id: _describe_segment(c.segment_json, tag_names) for c in rows}
    return _ctx(
        request,
        actor,
        campaigns=rows,
        tags=tags,
        locations=locations,
        segment_chips=segment_chips,
        **extra,
    )


def _campaign_or_none(db: Session, tenant_id: int, campaign_id: int) -> Campaign | None:
    return (
        tenant_query(db, Campaign, tenant_id).filter(Campaign.id == campaign_id).first()
    )


@router.get("/campaigns", response_class=HTMLResponse)
def campaigns_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.MARKETING_AUTO):
        return _feature_locked(
            request, actor, features_svc.MARKETING_AUTO, "行銷自動化"
        )
    return templates.TemplateResponse(
        "campaigns.html", _campaigns_ctx(request, actor, db)
    )


@router.post("/campaigns", response_class=HTMLResponse)
async def campaigns_create(
    request: Request,
    name: str = Form(...),
    type: str = Form(...),
    message_template: str = Form(...),
    schedule_at: str = Form(""),
    segment_tier: str = Form(""),
    segment_min_bookings: str = Form(""),
    segment_location_id: str = Form(""),
    segment_json: str = Form(""),
    reward_type: str = Form(""),
    reward_value: str = Form(""),
    message_type: str = Form("text"),
    flex_menu_id: str = Form(""),
    image_url: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    import json as _json

    if not _require_ui_feature(db, actor, features_svc.MARKETING_AUTO):
        return _feature_locked(
            request, actor, features_svc.MARKETING_AUTO, "行銷自動化"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        schedule = _parse_slot_start(schedule_at) if schedule_at.strip() else None
        # 受眾：表單選擇器組 dict；「進階原始 JSON」有填則優先（power-user 相容）。
        seg = segment_json.strip()
        if seg:
            _json.loads(seg)  # 驗證 JSON 合法
        else:
            form = await request.form()
            filters: dict = {}
            tag_ids = [
                int(v) for v in form.getlist("segment_tag_ids") if str(v).isdigit()
            ]
            if tag_ids:
                filters["tag_ids"] = tag_ids
            if segment_tier.strip():
                filters["tier"] = segment_tier.strip()
            mb = _opt_int(segment_min_bookings)
            if mb is not None:
                filters["min_bookings"] = mb
            loc = _opt_int(segment_location_id)
            if loc is not None:
                filters["location_id"] = loc
            seg = _json.dumps(filters, ensure_ascii=False) if filters else ""
        # 訊息型別（A3.2）：白名單外一律 text;image 需 https URL。
        mt = message_type if message_type in ("text", "flex", "image") else "text"
        img = image_url.strip() or None
        if mt == "image" and (img is None or not img.startswith("https://")):
            mt = "text"
        campaign = Campaign(
            tenant_id=tid,
            name=name,
            type=type,
            message_template=message_template,
            schedule_at=schedule,
            segment_json=seg or None,
            reward_type=reward_type or None,
            reward_value=_opt_int(reward_value),
            message_type=mt,
            flex_menu_id=_opt_int(flex_menu_id),
            image_url=img,
        )
        db.add(campaign)
        db.commit()
    except ValueError:
        db.rollback()
        error = "排程時間或受眾 JSON 格式錯誤"
    except HTTPException as exc:
        db.rollback()
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_campaigns_list.html", _campaigns_ctx(request, actor, db, error=error)
    )


@router.post("/campaigns/{campaign_id}/run", response_class=HTMLResponse)
def campaigns_run(
    request: Request,
    campaign_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
    push_client: LinePushClient = Depends(get_push_client),
):
    if not _require_ui_feature(db, actor, features_svc.MARKETING_AUTO):
        return _feature_locked(
            request, actor, features_svc.MARKETING_AUTO, "行銷自動化"
        )
    tid = actor.user.tenant_id
    error = None
    run_result = None
    campaign = _campaign_or_none(db, tid, campaign_id)
    if campaign is None:
        error = "活動不存在"
    else:
        run_result = marketing_svc.run_campaign(
            db,
            campaign=campaign,
            now=datetime.datetime.now(datetime.timezone.utc),
            cap=settings.marketing_max_per_run,
            push_client=push_client,
        )
    return templates.TemplateResponse(
        "_campaigns_list.html",
        _campaigns_ctx(request, actor, db, error=error, run_result=run_result),
    )


@router.post("/campaigns/{campaign_id}/deactivate", response_class=HTMLResponse)
def campaigns_deactivate(
    request: Request,
    campaign_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.MARKETING_AUTO):
        return _feature_locked(
            request, actor, features_svc.MARKETING_AUTO, "行銷自動化"
        )
    tid = actor.user.tenant_id
    campaign = _campaign_or_none(db, tid, campaign_id)
    if campaign is not None:
        campaign.is_active = False
        db.commit()
    return templates.TemplateResponse(
        "_campaigns_list.html", _campaigns_ctx(request, actor, db)
    )


@router.get("/campaigns/{campaign_id}/edit", response_class=HTMLResponse)
def campaigns_edit_form(
    request: Request,
    campaign_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.MARKETING_AUTO):
        return _feature_locked(
            request, actor, features_svc.MARKETING_AUTO, "行銷自動化"
        )
    return templates.TemplateResponse(
        "_campaigns_list.html",
        _campaigns_ctx(request, actor, db, editing_id=campaign_id),
    )


@router.post("/campaigns/{campaign_id}/update", response_class=HTMLResponse)
def campaigns_update(
    request: Request,
    campaign_id: int,
    name: str = Form(...),
    message_template: str = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.MARKETING_AUTO):
        return _feature_locked(
            request, actor, features_svc.MARKETING_AUTO, "行銷自動化"
        )
    tid = actor.user.tenant_id
    error = None
    editing_id = None
    try:
        marketing_svc.update_campaign(
            db,
            tenant_id=tid,
            campaign_id=campaign_id,
            name=name,
            message_template=message_template,
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_id = campaign_id
    return templates.TemplateResponse(
        "_campaigns_list.html",
        _campaigns_ctx(request, actor, db, error=error, editing_id=editing_id),
    )


@router.post("/campaigns/{campaign_id}/delete", response_class=HTMLResponse)
def campaigns_delete(
    request: Request,
    campaign_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.MARKETING_AUTO):
        return _feature_locked(
            request, actor, features_svc.MARKETING_AUTO, "行銷自動化"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        marketing_svc.delete_campaign(db, tenant_id=tid, campaign_id=campaign_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_campaigns_list.html", _campaigns_ctx(request, actor, db, error=error)
    )


# ── 店家自助：圖文選單卡片（FLEX_MENU） ─────────────────────────────────────────


def _get_or_create_flex_menu(db: Session, tenant_id: int) -> "flex_menu_svc.FlexMenu":
    menu = flex_menu_svc.get_active_menu(db, tenant_id=tenant_id)
    if menu is None:
        menus = flex_menu_svc.list_menus(db, tenant_id=tenant_id)
        menu = menus[0] if menus else flex_menu_svc.create_menu(db, tenant_id=tenant_id)
    return menu


def _flex_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    menu = _get_or_create_flex_menu(db, tid)
    cards = flex_menu_svc.list_cards(db, tenant_id=tid, menu_id=menu.id)
    preview = flex_menu_svc.build_flex_payload(menu, cards)
    return _ctx(
        request,
        actor,
        menu=menu,
        cards=cards,
        preview=preview,
        max_cards=flex_menu_svc.MAX_CARDS,
        **extra,
    )


@router.get("/flex-menu", response_class=HTMLResponse)
def flex_menu_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.FLEX_MENU):
        return _feature_locked(request, actor, features_svc.FLEX_MENU, "圖文選單卡片")
    return templates.TemplateResponse("flex_menu.html", _flex_ctx(request, actor, db))


@router.post("/flex-menu/title", response_class=HTMLResponse)
def flex_menu_set_title(
    request: Request,
    title: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.FLEX_MENU):
        return _feature_locked(request, actor, features_svc.FLEX_MENU, "圖文選單卡片")
    tid = actor.user.tenant_id
    menu = _get_or_create_flex_menu(db, tid)
    flex_menu_svc.update_menu(db, tenant_id=tid, menu_id=menu.id, title=title or "")
    return templates.TemplateResponse("_flex_menu.html", _flex_ctx(request, actor, db))


@router.post("/flex-menu/delete", response_class=HTMLResponse)
def flex_menu_delete(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """刪除整個選單（含所有卡片）；重繪時自動重建空選單＝重設。"""
    if not _require_ui_feature(db, actor, features_svc.FLEX_MENU):
        return _feature_locked(request, actor, features_svc.FLEX_MENU, "圖文選單卡片")
    tid = actor.user.tenant_id
    error = None
    menu = _get_or_create_flex_menu(db, tid)
    try:
        flex_menu_svc.delete_menu(db, tenant_id=tid, menu_id=menu.id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_flex_menu.html", _flex_ctx(request, actor, db, error=error)
    )


@router.post("/flex-menu/cards", response_class=HTMLResponse)
def flex_menu_add_card(
    request: Request,
    title: str = Form(...),
    action_type: str = Form(...),
    action_data: str = Form(...),
    subtitle: str = Form(""),
    image_url: str = Form(""),
    bg_color: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.FLEX_MENU):
        return _feature_locked(request, actor, features_svc.FLEX_MENU, "圖文選單卡片")
    tid = actor.user.tenant_id
    error = None
    menu = _get_or_create_flex_menu(db, tid)
    try:
        flex_menu_svc.add_card(
            db,
            tenant_id=tid,
            menu_id=menu.id,
            title=title,
            action_type=action_type,
            action_data=action_data,
            subtitle=subtitle or None,
            image_url=image_url or None,
            bg_color=bg_color or None,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_flex_menu.html", _flex_ctx(request, actor, db, error=error)
    )


@router.post("/flex-menu/cards/{card_id}/delete", response_class=HTMLResponse)
def flex_menu_delete_card(
    request: Request,
    card_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.FLEX_MENU):
        return _feature_locked(request, actor, features_svc.FLEX_MENU, "圖文選單卡片")
    tid = actor.user.tenant_id
    menu = _get_or_create_flex_menu(db, tid)
    error = None
    try:
        flex_menu_svc.delete_card(db, tenant_id=tid, menu_id=menu.id, card_id=card_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_flex_menu.html", _flex_ctx(request, actor, db, error=error)
    )


@router.get("/flex-menu/cards/{card_id}/edit", response_class=HTMLResponse)
def flex_menu_edit_card_form(
    request: Request,
    card_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.FLEX_MENU):
        return _feature_locked(request, actor, features_svc.FLEX_MENU, "圖文選單卡片")
    return templates.TemplateResponse(
        "_flex_menu.html", _flex_ctx(request, actor, db, editing_card_id=card_id)
    )


@router.post("/flex-menu/cards/{card_id}/update", response_class=HTMLResponse)
def flex_menu_update_card(
    request: Request,
    card_id: int,
    title: str = Form(...),
    action_type: str = Form(...),
    action_data: str = Form(...),
    subtitle: str = Form(""),
    image_url: str = Form(""),
    bg_color: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.FLEX_MENU):
        return _feature_locked(request, actor, features_svc.FLEX_MENU, "圖文選單卡片")
    tid = actor.user.tenant_id
    error = None
    editing_card_id = None
    menu = _get_or_create_flex_menu(db, tid)
    try:
        flex_menu_svc.update_card(
            db,
            tenant_id=tid,
            menu_id=menu.id,
            card_id=card_id,
            title=title,
            action_type=action_type,
            action_data=action_data,
            subtitle=subtitle,
            image_url=image_url,
            bg_color=bg_color,
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_card_id = card_id
    return templates.TemplateResponse(
        "_flex_menu.html",
        _flex_ctx(request, actor, db, error=error, editing_card_id=editing_card_id),
    )


# ── 店家自助：作品集（PUBLIC_PROFILE） ──────────────────────────────────────────


def _portfolio_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    return _ctx(
        request,
        actor,
        categories=portfolio_svc.list_categories(db, tenant_id=tid),
        items=portfolio_svc.list_items(db, tenant_id=tid),
        **extra,
    )


@router.get("/portfolio", response_class=HTMLResponse)
def portfolio_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PUBLIC_PROFILE):
        return _feature_locked(
            request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁"
        )
    return templates.TemplateResponse(
        "portfolio.html", _portfolio_ctx(request, actor, db)
    )


@router.post("/portfolio/categories", response_class=HTMLResponse)
def portfolio_create_category(
    request: Request,
    name: str = Form(...),
    sort_order: int = Form(0),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PUBLIC_PROFILE):
        return _feature_locked(
            request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        portfolio_svc.create_category(
            db, tenant_id=tid, name=name, sort_order=sort_order
        )
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_portfolio.html", _portfolio_ctx(request, actor, db, error=error)
    )


@router.post("/portfolio/categories/{category_id}/delete", response_class=HTMLResponse)
def portfolio_delete_category(
    request: Request,
    category_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PUBLIC_PROFILE):
        return _feature_locked(
            request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        portfolio_svc.delete_category(db, tenant_id=tid, category_id=category_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_portfolio.html", _portfolio_ctx(request, actor, db, error=error)
    )


@router.get("/portfolio/categories/{category_id}/edit", response_class=HTMLResponse)
def portfolio_edit_category_form(
    request: Request,
    category_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PUBLIC_PROFILE):
        return _feature_locked(
            request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁"
        )
    return templates.TemplateResponse(
        "_portfolio.html",
        _portfolio_ctx(request, actor, db, editing_category_id=category_id),
    )


@router.post("/portfolio/categories/{category_id}/update", response_class=HTMLResponse)
def portfolio_update_category(
    request: Request,
    category_id: int,
    name: str = Form(...),
    sort_order: int = Form(0),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PUBLIC_PROFILE):
        return _feature_locked(
            request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁"
        )
    tid = actor.user.tenant_id
    error = None
    editing_category_id = None
    try:
        portfolio_svc.update_category(
            db,
            tenant_id=tid,
            category_id=category_id,
            name=name,
            sort_order=sort_order,
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_category_id = category_id
    return templates.TemplateResponse(
        "_portfolio.html",
        _portfolio_ctx(
            request, actor, db, error=error, editing_category_id=editing_category_id
        ),
    )


@router.post("/portfolio/items", response_class=HTMLResponse)
def portfolio_create_item(
    request: Request,
    image_url: str = Form(...),
    caption: str = Form(""),
    category_id: str = Form(""),
    sort_order: int = Form(0),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PUBLIC_PROFILE):
        return _feature_locked(
            request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        portfolio_svc.create_item(
            db,
            tenant_id=tid,
            image_url=image_url,
            caption=caption or None,
            category_id=_opt_int(category_id),
            sort_order=sort_order,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "分類格式錯誤"
    return templates.TemplateResponse(
        "_portfolio.html", _portfolio_ctx(request, actor, db, error=error)
    )


@router.post("/portfolio/items/{item_id}/delete", response_class=HTMLResponse)
def portfolio_delete_item(
    request: Request,
    item_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PUBLIC_PROFILE):
        return _feature_locked(
            request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        portfolio_svc.delete_item(db, tenant_id=tid, item_id=item_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_portfolio.html", _portfolio_ctx(request, actor, db, error=error)
    )


@router.get("/portfolio/items/{item_id}/edit", response_class=HTMLResponse)
def portfolio_edit_item_form(
    request: Request,
    item_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PUBLIC_PROFILE):
        return _feature_locked(
            request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁"
        )
    return templates.TemplateResponse(
        "_portfolio.html", _portfolio_ctx(request, actor, db, editing_item_id=item_id)
    )


@router.post("/portfolio/items/{item_id}/update", response_class=HTMLResponse)
def portfolio_update_item(
    request: Request,
    item_id: int,
    image_url: str = Form(...),
    caption: str = Form(""),
    sort_order: int = Form(0),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PUBLIC_PROFILE):
        return _feature_locked(
            request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁"
        )
    tid = actor.user.tenant_id
    error = None
    editing_item_id = None
    try:
        portfolio_svc.update_item(
            db,
            tenant_id=tid,
            item_id=item_id,
            image_url=image_url,
            caption=caption,
            sort_order=sort_order,
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_item_id = item_id
    return templates.TemplateResponse(
        "_portfolio.html",
        _portfolio_ctx(
            request, actor, db, error=error, editing_item_id=editing_item_id
        ),
    )


# ── 店家自助：公開店家頁（PUBLIC_PROFILE） ──────────────────────────────────────


def _profile_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    return _ctx(
        request,
        actor,
        profile=profile_svc.get_by_tenant(db, tid),
        **extra,
    )


@router.get("/profile", response_class=HTMLResponse)
def profile_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PUBLIC_PROFILE):
        return _feature_locked(
            request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁"
        )
    return templates.TemplateResponse("profile.html", _profile_ctx(request, actor, db))


@router.post("/profile", response_class=HTMLResponse)
def profile_save(
    request: Request,
    slug: str = Form(...),
    display_name: str = Form(""),
    banner_url: str = Form(""),
    theme_color: str = Form(""),
    social_links: str = Form(""),
    seo_title: str = Form(""),
    seo_description: str = Form(""),
    intro: str = Form(""),
    announcement: str = Form(""),
    is_published: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PUBLIC_PROFILE):
        return _feature_locked(
            request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁"
        )
    tid = actor.user.tenant_id
    error = None
    saved = False
    try:
        profile_svc.upsert(
            db,
            tid,
            slug=slug,
            display_name=display_name or None,
            banner_url=banner_url or None,
            theme_color=theme_color or None,
            social_links=social_links or None,
            seo_title=seo_title or None,
            seo_description=seo_description or None,
            intro=intro or None,
            announcement=announcement or None,
            is_published=(is_published == "true"),
        )
        saved = True
    except profile_svc.SlugTakenError:
        error = "此網址代稱已被使用，請換一個。"
    except ValueError as exc:
        error = str(exc)
    return templates.TemplateResponse(
        "_profile.html", _profile_ctx(request, actor, db, error=error, saved=saved)
    )


