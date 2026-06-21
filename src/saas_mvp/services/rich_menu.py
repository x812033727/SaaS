"""Rich Menu（圖文選單）服務 — 預設模板 + 主題配色，套用至 LINE。

設計：
* 不引入影像函式庫——主題背景以**純 stdlib（zlib）產生純色 PNG**，零新依賴。
* 模板定義選單版型（size + 各區塊 bounds + postback action）；按鈕 action 直接
  對應既有預約對話 dispatcher（book / my / slots / help），故點按鈕即觸發預約流程。
* 套用四步：（刪舊）→ create → upload_image → set_default；richMenuId 存回
  LineChannelConfig。

店家如需自訂背景圖，未來可改傳上傳的圖片 bytes 取代純色 PNG（介面已支援 image bytes）。
"""

from __future__ import annotations

import struct
import zlib

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from saas_mvp.line_client import LineRichMenuClient, LineRichMenuError
from saas_mvp.models.tenant import Tenant

# ── 主題配色（name → RGB） ────────────────────────────────────────────────────
THEMES: dict[str, tuple[int, int, int]] = {
    "line_green": (6, 199, 85),
    "ocean_blue": (30, 136, 229),
    "royal_purple": (123, 31, 162),
    "sunset_orange": (245, 124, 0),
    "dark": (33, 33, 33),
}

# ── 按鈕（label, postback data）；data 對應 booking dispatcher action ──────────
_BTN_BOOK = ("預約", "action=book")
_BTN_MY = ("我的預約", "action=my")
_BTN_SLOTS = ("可預約時段", "action=slots")
_BTN_HELP = ("使用說明", "action=help")

# ── 模板（name → 版型）；grid = (cols, rows)，buttons 依列優先排列 ───────────────
TEMPLATES: dict[str, dict] = {
    "booking3": {
        "label": "三宮格（預約/我的預約/時段）",
        "size": {"width": 2500, "height": 843},
        "grid": (3, 1),
        "buttons": [_BTN_BOOK, _BTN_MY, _BTN_SLOTS],
    },
    "booking4": {
        "label": "四宮格（含使用說明）",
        "size": {"width": 2500, "height": 1686},
        "grid": (2, 2),
        "buttons": [_BTN_BOOK, _BTN_MY, _BTN_SLOTS, _BTN_HELP],
    },
}

_CHAT_BAR_TEXT = "選單"


# ── 純 stdlib 純色 PNG 產生器（無 PIL） ───────────────────────────────────────

def solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """產生指定尺寸的純色 PNG（RGB, 8-bit）。僅用 zlib + struct。"""
    r, g, b = rgb
    row = b"\x00" + bytes((r, g, b)) * width  # 每列：filter byte 0 + width 個像素
    raw = row * height
    compressed = zlib.compress(raw, 9)

    def _chunk(typ: bytes, data: bytes) -> bytes:
        body = typ + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # color type 2 = RGB
    return sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", compressed) + _chunk(b"IEND", b"")


def _areas(template: dict) -> list[dict]:
    """依 grid 與 buttons 計算各區塊 bounds + postback action。"""
    cols, rows = template["grid"]
    width = template["size"]["width"]
    height = template["size"]["height"]
    cell_w = width // cols
    cell_h = height // rows
    buttons = template["buttons"]
    areas = []
    for idx, (label, data) in enumerate(buttons):
        col = idx % cols
        row = idx // cols
        x = col * cell_w
        y = row * cell_h
        # 最後一欄/列補足像素，避免整除餘數留白
        w = (width - x) if col == cols - 1 else cell_w
        h = (height - y) if row == rows - 1 else cell_h
        areas.append(
            {
                "bounds": {"x": x, "y": y, "width": w, "height": h},
                "action": {"type": "postback", "data": data, "displayText": label},
            }
        )
    return areas


def build_rich_menu_payload(template_name: str, theme_name: str) -> tuple[dict, bytes]:
    """組 LINE rich menu 結構 + 主題背景圖 bytes。"""
    template = TEMPLATES[template_name]
    rgb = THEMES[theme_name]
    payload = {
        "size": template["size"],
        "selected": False,
        "name": f"{template_name}-{theme_name}",
        "chatBarText": _CHAT_BAR_TEXT,
        "areas": _areas(template),
    }
    image = solid_png(template["size"]["width"], template["size"]["height"], rgb)
    return payload, image


def _validate(template_name: str, theme_name: str) -> None:
    if template_name not in TEMPLATES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown template: {template_name!r}",
        )
    if theme_name not in THEMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown theme: {theme_name!r}",
        )


def _require_cfg(db: Session, tenant_id: int):
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found")
    cfg = tenant.line_channel_config
    if cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="line channel config not found; set up LINE first",
        )
    return cfg


def get_rich_menu_status(db: Session, tenant_id: int) -> dict:
    cfg = _require_cfg(db, tenant_id)
    return {
        "applied": bool(cfg.rich_menu_id),
        "rich_menu_id": cfg.rich_menu_id,
        "template": cfg.rich_menu_template,
        "theme": cfg.rich_menu_theme,
    }


def apply_rich_menu(
    db: Session,
    tenant_id: int,
    *,
    template: str,
    theme: str,
    client: LineRichMenuClient,
) -> dict:
    """建立並套用 rich menu；回傳狀態 dict。

    Raises 400（未知模板/主題）、404（無 LINE 設定）、502（LINE API 失敗）。
    """
    _validate(template, theme)
    cfg = _require_cfg(db, tenant_id)
    access_token = cfg.access_token

    payload, image = build_rich_menu_payload(template, theme)
    old_id = cfg.rich_menu_id
    try:
        # 先嘗試刪舊（best-effort：舊選單可能已不存在，刪除失敗不阻擋套用新選單）
        if old_id:
            try:
                client.delete(old_id, access_token=access_token)
            except LineRichMenuError:
                pass
        rich_menu_id = client.create(payload, access_token=access_token)
        client.upload_image(rich_menu_id, image, "image/png", access_token=access_token)
        client.set_default(rich_menu_id, access_token=access_token)
    except LineRichMenuError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LINE Rich Menu API failed: {exc}",
        ) from exc

    cfg.rich_menu_id = rich_menu_id
    cfg.rich_menu_template = template
    cfg.rich_menu_theme = theme
    db.commit()
    db.refresh(cfg)
    return get_rich_menu_status(db, tenant_id)


def clear_rich_menu(
    db: Session, tenant_id: int, *, client: LineRichMenuClient
) -> dict:
    """移除已套用的 rich menu（best-effort 刪 LINE 端）並清空欄位。"""
    cfg = _require_cfg(db, tenant_id)
    if cfg.rich_menu_id:
        try:
            client.delete(cfg.rich_menu_id, access_token=cfg.access_token)
        except LineRichMenuError:
            pass
    cfg.rich_menu_id = None
    cfg.rich_menu_template = None
    cfg.rich_menu_theme = None
    db.commit()
    db.refresh(cfg)
    return get_rich_menu_status(db, tenant_id)
