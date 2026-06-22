"""Flex 圖文選單服務層 — 選單 + 卡片 CRUD + LINE Flex carousel payload 組裝。

所有查詢走 tenant_query 強制隔離；查無/跨租戶一律 404，不洩漏 ID 存在性。
每個選單至多 12 張卡片（LINE carousel 上限）——超過於 service 層拋 422。
build_flex_payload 為純函式、離線可測，產出合法 LINE Flex「carousel」訊息 JSON。
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.models.flex_menu import FlexMenu
from saas_mvp.models.flex_menu_card import FlexMenuCard
from saas_mvp.services.tenants import tenant_query

# LINE Flex carousel 上限：最多 12 個 bubble。
MAX_CARDS = 12

# 合法 action 類型（對應 LINE Flex button action）。
_VALID_ACTION_TYPES = frozenset({"uri", "postback", "message"})


# ── 選單 CRUD ─────────────────────────────────────────────────────────────────

def _get_menu_or_404(db: Session, tenant_id: int, menu_id: int) -> FlexMenu:
    menu = (
        tenant_query(db, FlexMenu, tenant_id)
        .filter(FlexMenu.id == menu_id)
        .first()
    )
    if menu is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Flex menu not found"
        )
    return menu


def list_menus(db: Session, *, tenant_id: int) -> list[FlexMenu]:
    return (
        tenant_query(db, FlexMenu, tenant_id)
        .order_by(FlexMenu.id)
        .all()
    )


def get_menu(db: Session, *, tenant_id: int, menu_id: int) -> FlexMenu:
    return _get_menu_or_404(db, tenant_id, menu_id)


def get_active_menu(db: Session, *, tenant_id: int) -> FlexMenu | None:
    """取租戶最新一筆 is_active 選單（供 LINE delivery）。查無回 None。"""
    return (
        tenant_query(db, FlexMenu, tenant_id)
        .filter(FlexMenu.is_active.is_(True))
        .order_by(FlexMenu.id.desc())
        .first()
    )


def create_menu(
    db: Session, *, tenant_id: int, title: str | None = None, is_active: bool = True
) -> FlexMenu:
    menu = FlexMenu(tenant_id=tenant_id, title=title, is_active=is_active)
    db.add(menu)
    db.commit()
    db.refresh(menu)
    return menu


def update_menu(
    db: Session,
    *,
    tenant_id: int,
    menu_id: int,
    title: str | None = None,
    is_active: bool | None = None,
) -> FlexMenu:
    menu = _get_menu_or_404(db, tenant_id, menu_id)
    if title is not None:
        menu.title = title
    if is_active is not None:
        menu.is_active = is_active
    db.commit()
    db.refresh(menu)
    return menu


def delete_menu(db: Session, *, tenant_id: int, menu_id: int) -> None:
    menu = _get_menu_or_404(db, tenant_id, menu_id)
    db.delete(menu)
    db.commit()


# ── 卡片 CRUD ─────────────────────────────────────────────────────────────────

def _validate_action_type(action_type: str) -> None:
    if action_type not in _VALID_ACTION_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid action_type: {action_type!r}",
        )


def _get_card_or_404(
    db: Session, tenant_id: int, menu_id: int, card_id: int
) -> FlexMenuCard:
    card = (
        tenant_query(db, FlexMenuCard, tenant_id)
        .filter(FlexMenuCard.id == card_id, FlexMenuCard.menu_id == menu_id)
        .first()
    )
    if card is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Flex menu card not found"
        )
    return card


def list_cards(db: Session, *, tenant_id: int, menu_id: int) -> list[FlexMenuCard]:
    _get_menu_or_404(db, tenant_id, menu_id)
    return (
        tenant_query(db, FlexMenuCard, tenant_id)
        .filter(FlexMenuCard.menu_id == menu_id)
        .order_by(FlexMenuCard.sort_order, FlexMenuCard.id)
        .all()
    )


def _count_cards(db: Session, tenant_id: int, menu_id: int) -> int:
    return (
        tenant_query(db, FlexMenuCard, tenant_id)
        .filter(FlexMenuCard.menu_id == menu_id)
        .count()
    )


def add_card(
    db: Session,
    *,
    tenant_id: int,
    menu_id: int,
    title: str,
    action_type: str,
    action_data: str,
    subtitle: str | None = None,
    image_url: str | None = None,
    bg_color: str | None = None,
    icon: str | None = None,
    sort_order: int = 0,
) -> FlexMenuCard:
    """新增一張卡片；超過 12 張上限拋 422。"""
    _get_menu_or_404(db, tenant_id, menu_id)
    _validate_action_type(action_type)
    # 鎖父選單列（FOR UPDATE）序列化「卡片數檢查→新增」：消除 unlocked
    # check-then-act 競態（並發新增可同時通過 12 張檢查、雙雙寫入而超上限）。
    db.execute(
        select(FlexMenu)
        .where(FlexMenu.id == menu_id, FlexMenu.tenant_id == tenant_id)
        .with_for_update()
    ).scalar_one_or_none()
    if _count_cards(db, tenant_id, menu_id) >= MAX_CARDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Flex menu may contain at most {MAX_CARDS} cards",
        )
    card = FlexMenuCard(
        tenant_id=tenant_id,
        menu_id=menu_id,
        title=title,
        subtitle=subtitle,
        image_url=image_url,
        bg_color=bg_color,
        icon=icon,
        action_type=action_type,
        action_data=action_data,
        sort_order=sort_order,
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


def update_card(
    db: Session,
    *,
    tenant_id: int,
    menu_id: int,
    card_id: int,
    title: str | None = None,
    subtitle: str | None = None,
    image_url: str | None = None,
    bg_color: str | None = None,
    icon: str | None = None,
    action_type: str | None = None,
    action_data: str | None = None,
    sort_order: int | None = None,
) -> FlexMenuCard:
    card = _get_card_or_404(db, tenant_id, menu_id, card_id)
    if action_type is not None:
        _validate_action_type(action_type)
        card.action_type = action_type
    if title is not None:
        card.title = title
    if subtitle is not None:
        card.subtitle = subtitle
    if image_url is not None:
        card.image_url = image_url
    if bg_color is not None:
        card.bg_color = bg_color
    if icon is not None:
        card.icon = icon
    if action_data is not None:
        card.action_data = action_data
    if sort_order is not None:
        card.sort_order = sort_order
    db.commit()
    db.refresh(card)
    return card


def delete_card(
    db: Session, *, tenant_id: int, menu_id: int, card_id: int
) -> None:
    card = _get_card_or_404(db, tenant_id, menu_id, card_id)
    db.delete(card)
    db.commit()


# ── Flex carousel payload 組裝（純函式、離線可測） ────────────────────────────

def _card_action(card: FlexMenuCard) -> dict:
    """卡片 → LINE Flex button action。"""
    label = (card.title or "選擇")[:20]
    if card.action_type == "uri":
        return {"type": "uri", "label": label, "uri": card.action_data}
    if card.action_type == "message":
        return {"type": "message", "label": label, "text": card.action_data}
    # 預設 postback
    return {
        "type": "postback",
        "label": label,
        "data": card.action_data,
        "displayText": label,
    }


def _bubble(card: FlexMenuCard) -> dict:
    """單張卡片 → 一個 carousel bubble（hero 圖 + body 標題/副標 + footer 按鈕）。"""
    bubble: dict = {"type": "bubble"}

    if card.image_url:
        bubble["hero"] = {
            "type": "image",
            "url": card.image_url,
            "size": "full",
            "aspectRatio": "20:13",
            "aspectMode": "cover",
        }

    body_contents: list[dict] = [
        {
            "type": "text",
            "text": card.title or "",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
        }
    ]
    if card.subtitle:
        body_contents.append(
            {
                "type": "text",
                "text": card.subtitle,
                "size": "sm",
                "color": "#888888",
                "wrap": True,
            }
        )
    body: dict = {
        "type": "box",
        "layout": "vertical",
        "contents": body_contents,
    }
    if card.bg_color:
        body["backgroundColor"] = card.bg_color
    bubble["body"] = body

    bubble["footer"] = {
        "type": "box",
        "layout": "vertical",
        "contents": [
            {
                "type": "button",
                "style": "primary",
                "action": _card_action(card),
            }
        ],
    }
    return bubble


def build_flex_payload(menu: FlexMenu, cards: list[FlexMenuCard]) -> dict:
    """組出合法 LINE Flex「carousel」訊息 JSON（最多 12 bubble）。

    回傳 dict 形如::

        {"type": "flex", "altText": ..., "contents": {"type": "carousel",
         "contents": [<bubble>, ...]}}

    cards 依呼叫端排序傳入；多於 12 張只取前 12（carousel 上限）。
    """
    bubbles = [_bubble(c) for c in cards[:MAX_CARDS]]
    alt_text = (menu.title or "圖文選單")[:400]
    return {
        "type": "flex",
        "altText": alt_text,
        "contents": {
            "type": "carousel",
            "contents": bubbles,
        },
    }
