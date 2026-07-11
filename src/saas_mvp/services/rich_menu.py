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
    # 品牌主題（深墨綠 #1A5C4A × 琥珀金點綴）；對齊管理後台設計 token。
    "brand": (26, 92, 74),
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
    width: int,
    height: int,
    grid: tuple[int, int],
    base: tuple[int, int, int],
    rows_spec: list[int] | None = None,
) -> bytes:
    """產生「各格分區上色」的多色 PNG（vector 模式，純 stdlib zlib）。

    以 base 色為基準，依格索引在 RGB 三通道做不同位移，使每個按鈕區塊呈現
    可辨識的色塊邊界——刻意與 solid_png（單一純色）不同，輸出 bytes 必然相異。
    rows_spec 提供時（不等寬版型，如 [3, 4, 4]）各列依自身欄數分區，
    色塊邊界與 tap areas 一致。
    """

    def _cell_color(idx: int) -> tuple[int, int, int]:
        # 依格索引對 base 做有界位移，產生明顯但不溢位的分區色差。
        shift = (idx + 1) * 37
        r = (base[0] + shift) % 256
        g = (base[1] + shift * 2) % 256
        b = (base[2] + shift * 3) % 256
        return r, g, b

    # 同一 row-band 的每一列像素相同：先組每列「單列模板」，再依 y 重複，
    # 避免對每像素 Python 迴圈（2500×1686 會過慢）。
    if rows_spec:
        rows = max(1, len(rows_spec))
        per_row_cols = [max(1, n) for n in rows_spec]
    else:
        cols, rows = grid
        cols = max(1, cols)
        rows = max(1, rows)
        per_row_cols = [cols] * rows
    cell_h = height // rows

    row_templates: list[bytes] = []
    idx_offset = 0
    for ncols in per_row_cols:
        cell_w = width // ncols
        pixels = bytearray()
        for x in range(width):
            col_idx = min(ncols - 1, x // cell_w) if cell_w else 0
            r, g, b = _cell_color(idx_offset + col_idx)
            pixels += bytes((r, g, b))
        row_templates.append(b"\x00" + bytes(pixels))  # filter byte + 像素
        idx_offset += ncols

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


def _cell_boxes(template: dict) -> list[tuple[int, int, int, int]]:
    """各按鈕格框 (x, y, w, h)——tap areas 與畫字共用的唯一幾何來源。

    若模板帶 ``rows_spec``（每列欄數的清單，例如 [3, 4, 4] = 大尺寸 3+4+4），
    則以不等寬列鋪排（對標 vibeaico 大尺寸選單）；否則用均勻 grid。
    最後一欄/列補足整除餘數像素，避免留白。
    """
    width = template["size"]["width"]
    height = template["size"]["height"]
    buttons = template["buttons"]

    rows_spec = template.get("rows_spec")
    if rows_spec:
        boxes = []
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
                x = ci * cell_w
                w = (width - x) if ci == ncols - 1 else cell_w
                boxes.append((x, y, w, h))
                idx += 1
        return boxes

    cols, rows = template["grid"]
    cell_w = width // cols
    cell_h = height // rows
    boxes = []
    for idx in range(len(buttons)):
        col = idx % cols
        row = idx // cols
        x = col * cell_w
        y = row * cell_h
        w = (width - x) if col == cols - 1 else cell_w
        h = (height - y) if row == rows - 1 else cell_h
        boxes.append((x, y, w, h))
    return boxes


def _areas(template: dict) -> list[dict]:
    """依 _cell_boxes 幾何組各區塊 bounds + postback action。"""
    return [
        {
            "bounds": {"x": x, "y": y, "width": w, "height": h},
            "action": {"type": "postback", "data": data, "displayText": label},
        }
        for (x, y, w, h), (label, data) in zip(_cell_boxes(template), template["buttons"])
    ]


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


def _stroke_width(size: int) -> int:
    """描邊寬度公式——量測與繪製必須用同一套，否則描邊會吃掉留邊。"""
    return max(2, size // 18)


def _fit_font_size(
    draw, label: str, max_w: float, max_h: float, *, with_stroke: bool = True
) -> int:
    """求出能讓 label 完整塞進 (max_w, max_h) 的最大字級（量測實際寬高，非估算）。

    with_stroke=True 時量測含描邊後的實際外框。連最小字級（12）都塞不下時
    回 0，由呼叫端跳過畫字——絕不硬畫溢出。
    """
    lo, hi = 12, _LABEL_MAX_SIZE
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        font = _load_font(mid)
        if font is None:
            return 0
        sw = _stroke_width(mid) if with_stroke else 0
        x0, y0, x1, y1 = draw.textbbox((0, 0), label, font=font, stroke_width=sw)
        if (x1 - x0) <= max_w and (y1 - y0) <= max_h:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _lighten(rgb: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(min(255, int(c + (255 - c) * t)) for c in rgb)


def _darken(rgb: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(max(0, int(c * (1 - t))) for c in rgb)


def _luminance(rgb: tuple[int, int, int]) -> float:
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


# 卡片式渲染的每主題樣式。brand 主題顯式指定（對齊 app.css token：
# 墨綠 #1A5C4A / 深墨綠 #124436 / 暖白 #FAF9F6 / 琥珀金 #C4956A）；
# 其餘主題由底色亮度推導，維持「一個主題色即可用」的既有使用方式。
_BRAND_STYLE = {
    "bg_top": (26, 92, 74),
    "bg_bottom": (18, 68, 54),
    "card": (250, 249, 246),
    "card_shadow": (13, 48, 38),
    "text": (26, 92, 74),
    "icon": (196, 149, 106),
}


def _theme_style(theme_name: str | None, base_rgb: tuple[int, int, int]) -> dict:
    """卡片式渲染樣式：漸層上下色、卡面色、陰影色、文字色、圖示色。"""
    if theme_name == "brand":
        return dict(_BRAND_STYLE)
    if _luminance(base_rgb) > 90:  # 亮底主題 → 暖白卡 + 主題深色字
        return {
            "bg_top": _lighten(base_rgb, 0.12),
            "bg_bottom": _darken(base_rgb, 0.20),
            "card": (250, 249, 246),
            "card_shadow": _darken(base_rgb, 0.40),
            "text": _darken(base_rgb, 0.45),
            "icon": _darken(base_rgb, 0.15),
        }
    return {  # 暗底主題（如 dark）→ 微亮卡 + 淺色字
        "bg_top": _lighten(base_rgb, 0.10),
        "bg_bottom": _darken(base_rgb, 0.40),
        "card": _lighten(base_rgb, 0.16),
        "card_shadow": _darken(base_rgb, 0.55),
        "text": (245, 245, 243),
        "icon": (228, 228, 224),
    }


# postback data → 圖示種類（_cycle_buttons 循環填格時 data 不變，對應恆正確）。
_ICON_FOR_ACTION = {
    "action=book": "calendar",
    "action=my": "list",
    "action=slots": "clock",
    "action=help": "question",
}


def _draw_icon(draw, kind: str, cx: float, cy: float, size: float, color) -> None:
    """以基本幾何繪製簡易向量圖示（日曆/清單/時鐘/問號），隨格子尺寸縮放。"""
    s = size / 2.0
    lw = max(4, int(size) // 12)
    if kind == "calendar":
        x0, y0, x1, y1 = cx - s, cy - s * 0.85, cx + s, cy + s * 0.85
        r = max(4, int(size) // 10)
        draw.rounded_rectangle([x0, y0, x1, y1], radius=r, outline=color, width=lw)
        head_y = y0 + (y1 - y0) * 0.32
        draw.line([x0, head_y, x1, head_y], fill=color, width=lw)
        for fx in (0.30, 0.70):  # 頂部綁環
            bx = x0 + (x1 - x0) * fx
            draw.line([bx, y0 - s * 0.18, bx, y0 + s * 0.10], fill=color, width=lw)
        dot = max(3, int(size) // 14)  # 2x3 日期點陣
        for ry in (0.55, 0.78):
            for rx in (0.28, 0.5, 0.72):
                px = x0 + (x1 - x0) * rx
                py = y0 + (y1 - y0) * ry
                draw.ellipse([px - dot, py - dot, px + dot, py + dot], fill=color)
    elif kind == "list":
        x0, x1 = cx - s, cx + s
        dot = max(4, int(size) // 12)
        for fy in (-0.55, 0.0, 0.55):
            y = cy + s * fy
            draw.ellipse([x0 - dot, y - dot, x0 + dot, y + dot], fill=color)
            draw.line([x0 + dot * 3, y, x1, y], fill=color, width=lw)
    elif kind == "clock":
        draw.ellipse([cx - s, cy - s, cx + s, cy + s], outline=color, width=lw)
        draw.line([cx, cy, cx, cy - s * 0.55], fill=color, width=lw)  # 分針(12 點)
        draw.line([cx, cy, cx + s * 0.42, cy + s * 0.25], fill=color, width=lw)  # 時針(~4 點)
    else:  # question
        draw.arc(
            [cx - s * 0.6, cy - s, cx + s * 0.6, cy + s * 0.2],
            start=-210, end=55, fill=color, width=lw,
        )
        draw.line([cx, cy + s * 0.05, cx, cy + s * 0.45], fill=color, width=lw)
        dot = max(4, int(size) // 11)
        draw.ellipse([cx - dot, cy + s * 0.75 - dot, cx + dot, cy + s * 0.75 + dot], fill=color)


# 卡片式渲染參數。
_CARD_GAP = 28  # 格框內縮量（卡縫 = 2×GAP，露出漸層底作為分隔）
_CARD_TEXT_HEIGHT_RATIO = 0.24  # 文字量測高（相對卡高；圖示佔上半部）
_ICON_SIZE_RATIO = 0.30  # 圖示尺寸（相對卡短邊）


def _draw_labels(
    png_bytes: bytes,
    template: dict,
    *,
    base_rgb: tuple[int, int, int] | None = None,
    theme_name: str | None = None,
) -> bytes:
    """把按鈕內容畫到對應格子上。幾何一律取 _cell_boxes（與 tap areas 同源）。

    * base_rgb 提供時（template 模式）→ 卡片式精品風：上下漸層底 + 每格圓角
      卡片（卡縫露底作分隔）+ 向量圖示 + 文字。
    * base_rgb=None（vector 模式）→ 僅置中畫字（描邊 + 依底色亮度切字色），
      維持既有行為。
    * 字級以「實際量測 + 二分搜尋」縮到塞進留邊，全格取 min 統一字級；
      塞不下（回 0）則整體放棄。任何失敗都回傳原始 png_bytes（不破壞流程）。
    """
    if not _labels_enabled():
        return png_bytes
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return png_bytes
    try:
        buttons = template["buttons"]
        boxes = _cell_boxes(template)
        base_img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        width, height = base_img.size
        card_mode = base_rgb is not None

        # 1) 先量字級（用暫存畫布，不動底圖）——無字型/塞不下即回退，
        #    確保「回傳原始 bytes 不變」的契約在任何繪製發生前成立。
        measure = ImageDraw.Draw(Image.new("RGB", (8, 8)))
        sizes = []
        for (x, y, w, h), (label, _data) in zip(boxes, buttons):
            if card_mode:
                card_w = w - 2 * _CARD_GAP
                card_h = h - 2 * _CARD_GAP
                max_w = card_w * _LABEL_WIDTH_RATIO
                max_h = card_h * _CARD_TEXT_HEIGHT_RATIO
            else:
                max_w = w * _LABEL_WIDTH_RATIO
                max_h = h * _LABEL_HEIGHT_RATIO
            sizes.append(
                _fit_font_size(measure, label, max_w, max_h, with_stroke=not card_mode)
            )
        if not sizes or min(sizes) <= 0:
            return png_bytes  # 無字型或塞不下 → 整體放棄，回原底圖
        uniform_size = min(sizes)
        font = _load_font(uniform_size)
        if font is None:
            return png_bytes

        if card_mode:
            style = _theme_style(theme_name, base_rgb)
            # 2a) 上下漸層底（linear_gradient 為 C 實作，2500x1686 毫秒級）。
            mask = Image.linear_gradient("L").resize((width, height))
            img = Image.composite(
                Image.new("RGB", (width, height), style["bg_bottom"]),
                Image.new("RGB", (width, height), style["bg_top"]),
                mask,
            )
            draw = ImageDraw.Draw(img)
            # 2b) 每格：陰影 + 圓角卡 + 圖示 + 文字。
            for (x, y, w, h), (label, data) in zip(boxes, buttons):
                cx0, cy0 = x + _CARD_GAP, y + _CARD_GAP
                cx1, cy1 = x + w - _CARD_GAP, y + h - _CARD_GAP
                radius = max(24, min(cx1 - cx0, cy1 - cy0) // 14)
                draw.rounded_rectangle(
                    [cx0, cy0 + 8, cx1, cy1 + 8], radius=radius, fill=style["card_shadow"]
                )
                draw.rounded_rectangle(
                    [cx0, cy0, cx1, cy1], radius=radius, fill=style["card"]
                )
                ccx = (cx0 + cx1) / 2
                card_h = cy1 - cy0
                icon_kind = _ICON_FOR_ACTION.get(data)
                if icon_kind:
                    icon_size = min(cx1 - cx0, card_h) * _ICON_SIZE_RATIO
                    _draw_icon(
                        draw, icon_kind, ccx, cy0 + card_h * 0.38, icon_size, style["icon"]
                    )
                draw.text(
                    (ccx, cy0 + card_h * 0.74),
                    label,
                    font=font,
                    fill=style["text"],
                    anchor="mm",
                )
        else:
            # vector 模式：分區色塊上置中畫字（描邊 + 依底色亮度切字色）。
            img = base_img
            draw = ImageDraw.Draw(img)
            for (x, y, w, h), (label, _data) in zip(boxes, buttons):
                cx, cy = x + w // 2, y + h // 2
                if _luminance(img.getpixel((cx, cy))) > 150:
                    fill, stroke_fill = (33, 33, 33), (255, 255, 255)
                else:
                    fill, stroke_fill = (255, 255, 255), (0, 0, 0)
                draw.text(
                    (cx, cy),
                    label,
                    font=font,
                    fill=fill,
                    anchor="mm",
                    stroke_width=_stroke_width(uniform_size),
                    stroke_fill=stroke_fill,
                )

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
        # vector 已是分區色塊，只疊文字（base_rgb=None）。
        image = _draw_labels(
            _sectioned_png(
                width, height, template["grid"], rgb,
                rows_spec=template.get("rows_spec"),
            ),
            template,
        )
    else:
        image = _draw_labels(
            solid_png(width, height, rgb), template,
            base_rgb=rgb, theme_name=theme_name,
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
