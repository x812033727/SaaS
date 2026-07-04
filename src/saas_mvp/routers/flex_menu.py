"""Flex 圖文選單 router — 選單 + 卡片 CRUD + 預覽。

受認證 + 租戶隔離 + rate limit + require_feature(FLEX_MENU)；不掛 require_quota。
所有操作經 services/flex_menu.py。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from saas_mvp.deps import get_current_user, get_db, require_rate_limit
from saas_mvp.models.user import User
from saas_mvp.services import flex_menu as flex_svc
from saas_mvp.services.features import FLEX_MENU, require_feature

router = APIRouter(
    prefix="/booking/flex-menu",
    tags=["booking-flex-menu"],
    dependencies=[
        Depends(require_rate_limit),
        Depends(require_feature(FLEX_MENU)),
    ],
)


# ─────────────────────────────── Schemas ─────────────────────────────────────

class MenuCreate(BaseModel):
    title: str | None = Field(default=None, max_length=128)
    is_active: bool = True


class MenuUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=128)
    is_active: bool | None = None


class MenuResponse(BaseModel):
    id: int
    tenant_id: int
    title: str | None
    is_active: bool

    model_config = {"from_attributes": True}


class CardCreate(BaseModel):
    title: str = Field(max_length=128)
    action_type: str = Field(max_length=16)
    action_data: str = Field(max_length=512)
    subtitle: str | None = Field(default=None, max_length=256)
    image_url: str | None = Field(default=None, max_length=512)
    bg_color: str | None = Field(default=None, max_length=16)
    icon: str | None = Field(default=None, max_length=32)
    sort_order: int = 0


class CardUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=128)
    action_type: str | None = Field(default=None, max_length=16)
    action_data: str | None = Field(default=None, max_length=512)
    subtitle: str | None = Field(default=None, max_length=256)
    image_url: str | None = Field(default=None, max_length=512)
    bg_color: str | None = Field(default=None, max_length=16)
    icon: str | None = Field(default=None, max_length=32)
    sort_order: int | None = None


class CardResponse(BaseModel):
    id: int
    tenant_id: int
    menu_id: int
    sort_order: int
    title: str
    subtitle: str | None
    image_url: str | None
    bg_color: str | None
    icon: str | None
    action_type: str
    action_data: str

    model_config = {"from_attributes": True}


# ─────────────────────────────── Menus ───────────────────────────────────────

@router.post("/", response_model=MenuResponse, status_code=status.HTTP_201_CREATED)
def create_menu(
    body: MenuCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MenuResponse:
    menu = flex_svc.create_menu(
        db, tenant_id=current_user.tenant_id, title=body.title, is_active=body.is_active
    )
    return MenuResponse.model_validate(menu)


@router.get("/", response_model=list[MenuResponse])
def list_menus(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[MenuResponse]:
    rows = flex_svc.list_menus(db, tenant_id=current_user.tenant_id)
    return [MenuResponse.model_validate(r) for r in rows]


@router.get("/{menu_id}", response_model=MenuResponse)
def get_menu(
    menu_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MenuResponse:
    menu = flex_svc.get_menu(db, tenant_id=current_user.tenant_id, menu_id=menu_id)
    return MenuResponse.model_validate(menu)


@router.put("/{menu_id}", response_model=MenuResponse)
def update_menu(
    menu_id: int,
    body: MenuUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MenuResponse:
    menu = flex_svc.update_menu(
        db,
        tenant_id=current_user.tenant_id,
        menu_id=menu_id,
        title=body.title,
        is_active=body.is_active,
    )
    return MenuResponse.model_validate(menu)


@router.delete(
    "/{menu_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response
)
def delete_menu(
    menu_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    flex_svc.delete_menu(db, tenant_id=current_user.tenant_id, menu_id=menu_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ─────────────────────────────── Cards ───────────────────────────────────────

@router.get("/{menu_id}/cards", response_model=list[CardResponse])
def list_cards(
    menu_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[CardResponse]:
    rows = flex_svc.list_cards(db, tenant_id=current_user.tenant_id, menu_id=menu_id)
    return [CardResponse.model_validate(r) for r in rows]


@router.post(
    "/{menu_id}/cards",
    response_model=CardResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_card(
    menu_id: int,
    body: CardCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CardResponse:
    card = flex_svc.add_card(
        db,
        tenant_id=current_user.tenant_id,
        menu_id=menu_id,
        title=body.title,
        action_type=body.action_type,
        action_data=body.action_data,
        subtitle=body.subtitle,
        image_url=body.image_url,
        bg_color=body.bg_color,
        icon=body.icon,
        sort_order=body.sort_order,
    )
    return CardResponse.model_validate(card)


@router.get("/{menu_id}/cards/{card_id}", response_model=CardResponse)
def get_card(
    menu_id: int,
    card_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CardResponse:
    card = flex_svc.get_card(
        db, tenant_id=current_user.tenant_id, menu_id=menu_id, card_id=card_id
    )
    return CardResponse.model_validate(card)


@router.put("/{menu_id}/cards/{card_id}", response_model=CardResponse)
def update_card(
    menu_id: int,
    card_id: int,
    body: CardUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CardResponse:
    card = flex_svc.update_card(
        db,
        tenant_id=current_user.tenant_id,
        menu_id=menu_id,
        card_id=card_id,
        title=body.title,
        subtitle=body.subtitle,
        image_url=body.image_url,
        bg_color=body.bg_color,
        icon=body.icon,
        action_type=body.action_type,
        action_data=body.action_data,
        sort_order=body.sort_order,
    )
    return CardResponse.model_validate(card)


@router.delete(
    "/{menu_id}/cards/{card_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_card(
    menu_id: int,
    card_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    flex_svc.delete_card(
        db, tenant_id=current_user.tenant_id, menu_id=menu_id, card_id=card_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ─────────────────────────────── Preview ─────────────────────────────────────

@router.get("/{menu_id}/preview")
def preview_menu(
    menu_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """回傳組好的 LINE Flex carousel payload（供前端預覽）。"""
    menu = flex_svc.get_menu(db, tenant_id=current_user.tenant_id, menu_id=menu_id)
    cards = flex_svc.list_cards(
        db, tenant_id=current_user.tenant_id, menu_id=menu_id
    )
    return flex_svc.build_flex_payload(menu, cards)
