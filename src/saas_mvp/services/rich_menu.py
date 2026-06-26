"""Rich Menu（圖文選單）服務 — 預設模板 + 主題配色，套用至 LINE。

設計：
* 底圖以**純 stdlib（zlib）產生純色／分區色塊 PNG**，零強制依賴。
* 產生底圖後，若環境具備 Pillow + 中文字型，再把各按鈕的文字標籤（預約／我的預約
  …）畫到對應格子上，讓 template／vector 模式「選了就有字」。此步為 best-effort：
  缺 Pillow 或字型時靜默跳過、回傳純色底圖（行為與舊版相容，永不因此失敗）。
* 模板定義選單版型（size + 各區塊 bounds + postback action）；按鈕 action 直接
  對應既有預約對話 dispatcher（book / my / slots / help），故點按鈕即觸發預約流程。
* 套用四步：（刪舊）→ create → upload_image → set_default；richMenuId 存回
  LineChannelConfig。

店家如需自訂背景圖，custom_image 模式直接上傳含文字的 PNG（不再經自動印字）。
"""

from __future__ import annotations

import io
import os
import struct
import zlib
from functools import lru_cache

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
    "rose_pink": (233, 30, 99),
    # 精品主題（香檳金）；對標 vibeaico「BOUTIQUE 精品主題」。
    "boutique": (191, 167, 106),
}

# ── 建立模式（management UI 三選一） ──────────────────────────────────────────
#   template     ：主題純色 PNG 背景（預設，向後相容）。
#   custom_image ：店家自備背景圖 bytes（image_bytes 覆蓋產生的 PNG）。
#   vector       ：以 stdlib zlib 產生「各按鈕分區上色」的多色 PNG（非純色）。
MODE_TEMPLATE = "template"
MODE_CUSTOM_IMAGE = "custom_image"
MODE_VECTOR = "vector"
MODES: dict[str, str] = {
    MODE_TEMPLATE: "主題模板（純色背景）",
    MODE_CUSTOM_IMAGE: "自訂背景圖",
    MODE_VECTOR: "分區色塊（自動生成）",
}

# ── 按鈕（label, postback data）；data 對應 booking dispatcher action ──────────
_BTN_BOOK = ("預約", "action=book")
_BTN_MY = ("我的預約", "action=my")
_BTN_SLOTS = ("可預約時段", "action=slots")
_BTN_HELP = ("使用說明", "action=help")

# ── 模板（name → 版型）；grid = (cols, rows)，buttons 依列優先排列 ───────────────
# 至少 7 種版型（1x1 / 1x2 / 2x1 / 2x2 / 2x3 / 3x2 / 1x3），按鈕依格數循環取用。
def _cycle_buttons(n: int) -> list[tuple[str, str]]:
    """取 n 個按鈕（不足時循環既有四顆，確保每格皆有 action）。"""
    base = [_BTN_BOOK, _BTN_SLOTS, _BTN_MY, _BTN_HELP]
    return [base[i % len(base)] for i in range(n)]


TEMPLATES: dict[str, dict] = {
    # 既有兩種版型維持原 key 與按鈕配置（向後相容，既有測試/UI 不破）。
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
    # 新增格狀版型。
    "grid1x1": {
        "label": "單格（僅預約）",
        "size": {"width": 2500, "height": 843},
        "grid": (1, 1),
        "buttons": _cycle_buttons(1),
    },
    "grid1x2": {
        "label": "左右兩格",
        "size": {"width": 2500, "height": 843},
        "grid": (2, 1),
        "buttons": _cycle_buttons(2),
    },
    "grid2x1": {
        "label": "上下兩格",
        "size": {"width": 2500, "height": 1686},
        "grid": (1, 2),
        "buttons": _cycle_buttons(2),
    },
    "grid1x3": {
        "label": "三欄橫列",
        "size": {"width": 2500, "height": 843},
        "grid": (3, 1),
        "buttons": _cycle_buttons(3),
    },
    "grid2x3": {
        "label": "六宮格（2 列 3 欄）",
        "size": {"width": 2500, "height": 1686},
        "grid": (3, 2),
        "buttons": _cycle_buttons(6),
    },
    "grid3x2": {
        "label": "六宮格（3 列 2 欄）",
        "size": {"width": 2500, "height": 1686},
        "grid": (2, 3),
        "buttons": _cycle_buttons(6),
    },
    # 大尺寸不等寬版型（3+4+4 = 11 區）；對標 vibeaico「大尺寸選單（3+4+4 等）」。
    # grid 僅供 vector 模式分區底圖近似；實際 tap 區由 rows_spec 鋪排。
    "grid3x4x4": {
        "label": "大尺寸（3+4+4，11 區）",
        "size": {"width": 2500, "height": 1686},
        "grid": (4, 3),
        "rows_spec": [3, 4, 4],
        "buttons": _cycle_buttons(11),
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


def _sectioned_png(
    width: int, height: int, grid: tuple[int, int], base: tuple[int, int, int]
) -> bytes:
    """產生「各格分區上色」的多色 PNG（vector 模式，純 stdlib zlib）。

    以 base 色為基準，依格索引在 RGB 三通道做不同位移，使每個按鈕區塊呈現
    可辨識的色塊邊界——刻意與 solid_png（單一純色）不同，輸出 bytes 必然相異。
    """
    cols, rows = grid
    cols = max(1, cols)
    rows = max(1, rows)
    cell_w = width // cols
    cell_h = height // rows

    def _cell_color(col: int, row: int) -> tuple[int, int, int]:
        idx = row * cols + col
        # 依格索引對 base 做有界位移，產生明顯但不溢位的分區色差。
        shift = (idx + 1) * 37
        r = (base[0] + shift) % 256
        g = (base[1] + shift * 2) % 256
        b = (base[2] + shift * 3) % 256
        return r, g, b

    # 同一 row-band 的每一列像素相同：先組 rows 種「單列模板」，再依 y 重複，
    # 避免對每像素 Python 迴圈（2500×1686 會過慢）。
    row_templates: list[bytes] = []
    for row_idx in range(rows):
        pixels = bytearray()
        for x in range(width):
            col_idx = min(cols - 1, x // cell_w) if cell_w else 0
            r, g, b = _cell_color(col_idx, row_idx)
            pixels += bytes((r, g, b))
        row_templates.append(b"\x00" + bytes(pixels))  # filter byte + 像素

    raw = bytearray()
    for y in range(height):
        row_idx = min(rows - 1, y // cell_h) if cell_h else 0
        raw += row_templates[row_idx]
    compressed = zlib.compress(bytes(raw), 9)

    def _chunk(typ: bytes, data: bytes) -> bytes:
        body = typ + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", compressed) + _chunk(b"IEND", b"")


def list_themes() -> list[str]:
    """可用主題 key 清單（management UI/API）。"""
    return list(THEMES.keys())


def list_templates() -> list[str]:
    """可用版型 key 清單（management UI/API）。"""
    return list(TEMPLATES.keys())


def list_modes() -> list[str]:
    """可用建立模式 key 清單（management UI/API）。"""
    return list(MODES.keys())


def _areas(template: dict) -> list[dict]:
    """依 grid 與 buttons 計算各區塊 bounds + postback action。

    若模板帶 ``rows_spec``（每列欄數的清單，例如 [3, 4, 4] = 大尺寸 3+4+4），
    則以不等寬列鋪排（對標 vibeaico 大尺寸選單）；否則用均勻 grid。
    """
    width = template["size"]["width"]
    height = template["size"]["height"]
    buttons = template["buttons"]

    rows_spec = template.get("rows_spec")
    if rows_spec:
        areas = []
        n_rows = len(rows_spec)
        cell_h = height // n_rows
        idx = 0
        for ri, ncols in enumerate(rows_spec):
            y = ri * cell_h
            h = (height - y) if ri == n_rows - 1 else cell_h
            cell_w = width // ncols
            for ci in range(ncols):
                if idx >= len(buttons):
                    break
                label, data = buttons[idx]
                x = ci * cell_w
                w = (width - x) if ci == ncols - 1 else cell_w
                areas.append({
                    "bounds": {"x": x, "y": y, "width": w, "height": h},
                    "action": {"type": "postback", "data": data, "displayText": label},
                })
                idx += 1
        return areas

    cols, rows = template["grid"]
    cell_w = width // cols
    cell_h = height // rows
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


# ── 自動文字標籤（best-effort，需 Pillow + 中文字型；缺則靜默跳過） ───────────
# 預設候選字型路徑（Debian/Ubuntu 的 Noto CJK 與文泉驛）；可用環境變數覆寫。
_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
)


def _labels_enabled() -> bool:
    """環境開關（預設開）。設 SAAS_RICH_MENU_LABELS=0/false 可關閉自動印字。"""
    return os.getenv("SAAS_RICH_MENU_LABELS", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _font_paths() -> list[str]:
    env = os.getenv("SAAS_RICH_MENU_FONT")
    paths = [env] if env else []
    paths.extend(_FONT_CANDIDATES)
    return paths


@lru_cache(maxsize=64)
def _load_font(size: int):
    """載入第一個可用的中文字型（依 size），全部失敗回 None。結果快取避免重複磁碟 I/O。"""
    try:
        from PIL import ImageFont
    except ImportError:
        return None
    for path in _font_paths():
        if not path or not os.path.exists(path):
            continue
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return None


# 文字佔格子的最大比例（留邊，避免貼齊格線或溢出到相鄰格）。
_LABEL_WIDTH_RATIO = 0.82
_LABEL_HEIGHT_RATIO = 0.46
_LABEL_MAX_SIZE = 180  # 字級上限，避免短標籤（如「預約」）被放到佔滿整格


def _fit_font_size(draw, label: str, max_w: float, max_h: float) -> int:
    """求出能讓 label 完整塞進 (max_w, max_h) 的最大字級（量測實際寬高，非估算）。"""
    lo, hi = 12, _LABEL_MAX_SIZE
    best = lo
    while lo <= hi:
        mid = (lo + hi) // 2
        font = _load_font(mid)
        if font is None:
            return 0
        x0, y0, x1, y1 = draw.textbbox((0, 0), label, font=font)
        if (x1 - x0) <= max_w and (y1 - y0) <= max_h:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _separator_color(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """格線顏色：底色亮則調暗、底色暗則調亮，確保分隔線在任何主題都看得見。"""
    luminance = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
    factor = 0.62 if luminance > 90 else 1.0
    if luminance <= 90:  # 深底 → 提亮
        return tuple(min(255, int(c + (255 - c) * 0.35)) for c in rgb)
    return tuple(int(c * factor) for c in rgb)


def _draw_labels(
    png_bytes: bytes,
    template: dict,
    *,
    base_rgb: tuple[int, int, int] | None = None,
) -> bytes:
    """把各按鈕文字置中畫到對應格子上，並畫出格線分隔。

    * 字級以「實際量測 + 二分搜尋」自動縮到剛好塞進格子（含留邊），徹底避免長標籤
      （如「可預約時段」）溢出到相鄰格。
    * 以 anchor="mm" 讓文字在格子正中（水平垂直皆置中）。
    * base_rgb 提供時於格線畫分隔線，讓多格選單看起來像獨立按鈕。
    * 任何失敗都回傳原始 png_bytes（不破壞流程）。
    """
    if not _labels_enabled():
        return png_bytes
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return png_bytes
    try:
        cols, rows = template["grid"]
        buttons = template["buttons"]
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        width, height = img.size
        cell_w = width // cols
        cell_h = height // rows
        draw = ImageDraw.Draw(img)

        # 1) 格線分隔（在文字之前畫，文字才會疊在最上層）。
        if base_rgb is not None and (cols > 1 or rows > 1):
            line_color = _separator_color(base_rgb)
            line_w = max(3, min(cell_w, cell_h) // 130)
            for c in range(1, cols):
                gx = c * cell_w
                draw.rectangle([gx - line_w // 2, 0, gx + line_w // 2, height], fill=line_color)
            for r in range(1, rows):
                gy = r * cell_h
                draw.rectangle([0, gy - line_w // 2, width, gy + line_w // 2], fill=line_color)

        # 2) 先算每格能容納的字級，取「全體最小」作為統一字級——讓長短標籤
        #    視覺一致（不會「預約」特別大、「可預約時段」特別小）。
        def _cell_box(idx: int):
            col = idx % cols
            row = idx // cols
            x = col * cell_w
            y = row * cell_h
            w = (width - x) if col == cols - 1 else cell_w
            h = (height - y) if row == rows - 1 else cell_h
            return x, y, w, h

        sizes = []
        for idx, (label, _data) in enumerate(buttons):
            _, _, w, h = _cell_box(idx)
            sizes.append(
                _fit_font_size(
                    draw, label, w * _LABEL_WIDTH_RATIO, h * _LABEL_HEIGHT_RATIO
                )
            )
        if not sizes or min(sizes) <= 0:
            return png_bytes  # 無字型 → 整體放棄，回純色底圖
        uniform_size = min(sizes)
        font = _load_font(uniform_size)

        # 3) 統一字級畫上各格文字（anchor=mm 水平垂直置中）。
        drew_any = False
        for idx, (label, _data) in enumerate(buttons):
            x, y, w, h = _cell_box(idx)
            size = uniform_size
            cx, cy = x + w // 2, y + h // 2
            # 依格子中心底色亮度決定字色（深底白字／淺底深字）。
            br, bg_, bb = img.getpixel((cx, cy))
            luminance = 0.299 * br + 0.587 * bg_ + 0.114 * bb
            fill = (33, 33, 33) if luminance > 150 else (255, 255, 255)
            stroke_fill = (255, 255, 255) if fill == (33, 33, 33) else (0, 0, 0)
            draw.text(
                (cx, cy),
                label,
                font=font,
                fill=fill,
                anchor="mm",
                stroke_width=max(2, size // 18),
                stroke_fill=stroke_fill,
            )
            drew_any = True
        if not drew_any:
            return png_bytes
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception:
        # 任何繪圖例外都回退到原圖——印字是加值，不能讓它擋住選單套用。
        return png_bytes


def build_rich_menu_payload(
    template_name: str,
    theme_name: str,
    *,
    mode: str = MODE_TEMPLATE,
    image_bytes: bytes | None = None,
) -> tuple[dict, bytes]:
    """組 LINE rich menu 結構 + 背景圖 bytes。

    mode：
      * template     ：主題純色 PNG（預設）。
      * custom_image ：使用呼叫端傳入的 image_bytes 取代產生的 PNG。
      * vector       ：以 stdlib zlib 產生分區色塊 PNG（與純色不同）。
    """
    template = TEMPLATES[template_name]
    rgb = THEMES[theme_name]
    width = template["size"]["width"]
    height = template["size"]["height"]
    payload = {
        "size": template["size"],
        "selected": False,
        "name": f"{template_name}-{theme_name}-{mode}"[:300],
        "chatBarText": _CHAT_BAR_TEXT,
        "areas": _areas(template),
    }
    if mode == MODE_CUSTOM_IMAGE:
        if not image_bytes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="custom_image mode requires image_bytes",
            )
        image = image_bytes
    elif mode == MODE_VECTOR:
        # vector 已是分區色塊，不再額外畫格線（base_rgb=None）。
        image = _draw_labels(
            _sectioned_png(width, height, template["grid"], rgb), template
        )
    else:
        image = _draw_labels(
            solid_png(width, height, rgb), template, base_rgb=rgb
        )
    return payload, image


def _validate(template_name: str, theme_name: str, mode: str = MODE_TEMPLATE) -> None:
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
    if mode not in MODES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown mode: {mode!r}",
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
    mode: str = MODE_TEMPLATE,
    image_bytes: bytes | None = None,
) -> dict:
    """建立並套用 rich menu；回傳狀態 dict。

    mode：template（純色）/ custom_image（image_bytes）/ vector（分區色塊）。

    Raises 400（未知模板/主題/模式、custom_image 缺圖）、404（無 LINE 設定）、
    502（LINE API 失敗）。
    """
    _validate(template, theme, mode)
    cfg = _require_cfg(db, tenant_id)
    access_token = cfg.access_token

    payload, image = build_rich_menu_payload(
        template, theme, mode=mode, image_bytes=image_bytes
    )
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
