"""Rich Menu 管理 API router — 列舉主題/版型/模式 + 套用（含三模式）+ 清除。

受認證 + 租戶隔離 + rate limit；不掛 require_feature（沿用 rich menu 既有無 flag
慣例，伺服器渲染 UI 也未額外 gate）。background 圖以 stdlib 產生，custom_image
模式接受 base64 上傳圖。所有套用經 services/rich_menu.py，LINE client 由 DI 注入。
"""

from __future__ import annotations

import base64
import binascii

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from saas_mvp.deps import get_current_user, get_db, require_rate_limit
from saas_mvp.line_client import LineRichMenuClient, get_rich_menu_client
from saas_mvp.models.user import User
from saas_mvp.services import rich_menu as rich_menu_svc

router = APIRouter(
    prefix="/booking/rich-menu",
    tags=["booking-rich-menu"],
    dependencies=[Depends(require_rate_limit)],
)


class ApplyBody(BaseModel):
    template: str
    theme: str
    mode: str = rich_menu_svc.MODE_TEMPLATE
    # custom_image 模式：base64 編碼的背景圖 bytes（PNG/JPEG）。
    image_base64: str | None = None


@router.get("/options")
def options() -> dict:
    """回傳可用的主題 / 版型 / 模式（供管理 UI 下拉）。

    template_labels 為增量欄位(console 顯示用),既有 key 清單形狀不變。
    """
    return {
        "themes": rich_menu_svc.list_themes(),
        "templates": rich_menu_svc.list_templates(),
        "modes": rich_menu_svc.list_modes(),
        "template_labels": {
            key: spec["label"] for key, spec in rich_menu_svc.TEMPLATES.items()
        },
    }


@router.get("/status")
def get_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    return rich_menu_svc.get_rich_menu_status(db, current_user.tenant_id)


@router.post("/apply")
def apply(
    body: ApplyBody,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    client: LineRichMenuClient = Depends(get_rich_menu_client),
) -> dict:
    image_bytes: bytes | None = None
    if body.mode == rich_menu_svc.MODE_CUSTOM_IMAGE:
        if not body.image_base64:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="custom_image mode requires image_base64",
            )
        try:
            image_bytes = base64.b64decode(body.image_base64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="image_base64 is not valid base64",
            )
    return rich_menu_svc.apply_rich_menu(
        db,
        current_user.tenant_id,
        template=body.template,
        theme=body.theme,
        client=client,
        mode=body.mode,
        image_bytes=image_bytes,
    )


@router.post("/clear")
def clear(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    client: LineRichMenuClient = Depends(get_rich_menu_client),
) -> dict:
    return rich_menu_svc.clear_rich_menu(db, current_user.tenant_id, client=client)


@router.get("/preview.png")
def preview(
    template: str,
    theme: str,
    current_user: User = Depends(get_current_user),
) -> Response:
    """套用前預覽:即席產生選單圖(不動 DB、不打 LINE;鏡射 /ui 版)。"""
    rich_menu_svc._validate(template, theme)
    _, image = rich_menu_svc.build_rich_menu_payload(template, theme)
    return Response(
        content=image,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )
