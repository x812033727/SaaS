"""Rich Menu 自動文字標籤測試 — 印字、字色對比、環境開關、缺字型/Pillow 回退。

設計重點：自動印字是 best-effort 加值功能，**任何缺件都必須回退到純色底圖**，
絕不能讓選單套用失敗。以下測試同時覆蓋「有字型時真的有畫」與「缺件時安全回退」。
"""

from __future__ import annotations

import io
import struct

import pytest

from saas_mvp.services import rich_menu as rm

# 字型是否可用（host CI 多半有 Noto/wqy；容器靠 Dockerfile 裝）。無字型則跳過
# 「真的有畫」這類斷言，只驗回退行為。
_FONT = rm._load_font(48)
_HAS_FONT = _FONT is not None
_needs_font = pytest.mark.skipif(not _HAS_FONT, reason="無中文字型，跳過印字斷言")


def _png_dims(png: bytes) -> tuple[int, int]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", png[16:24])


class TestFitNoSpill:
    """文字不可跨區：量測自適應字級必須讓文字寬高塞進格子留邊範圍內。"""

    @_needs_font
    def test_fit_font_size_respects_bounds(self):
        from PIL import Image, ImageDraw

        draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        max_w, max_h = 400.0, 200.0
        for label in ["預約", "我的預約", "可預約時段", "使用說明"]:
            size = rm._fit_font_size(draw, label, max_w, max_h)
            assert size > 0
            font = rm._load_font(size)
            x0, y0, x1, y1 = draw.textbbox((0, 0), label, font=font)
            assert (x1 - x0) <= max_w
            assert (y1 - y0) <= max_h

    @_needs_font
    def test_longest_label_stays_within_its_cell(self):
        """booking3 最長標籤「可預約時段」的字寬必須 < 單格寬度（不溢到隔壁格）。"""
        from PIL import Image, ImageDraw

        tmpl = rm.TEMPLATES["booking3"]
        cols = tmpl["grid"][0]
        cell_w = tmpl["size"]["width"] // cols
        draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        # 與 _draw_labels 同比例求字級，量測最長標籤實際寬度。
        size = rm._fit_font_size(
            draw, "可預約時段", cell_w * rm._LABEL_WIDTH_RATIO, 9999
        )
        font = rm._load_font(size)
        x0, _, x1, _ = draw.textbbox((0, 0), "可預約時段", font=font)
        assert (x1 - x0) < cell_w

    @_needs_font
    def test_uniform_font_size_across_cells(self):
        """同一選單各格採統一字級——以「線上可訂 booking4 全部標籤」量測一致性。

        間接驗證：渲染後不因每格字級不同而參差（透過 _fit_font_size 全體取 min）。
        """
        from PIL import Image, ImageDraw

        tmpl = rm.TEMPLATES["booking4"]
        cols, rows = tmpl["grid"]
        cell_w = tmpl["size"]["width"] // cols
        cell_h = tmpl["size"]["height"] // rows
        draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        sizes = [
            rm._fit_font_size(
                draw, label,
                cell_w * rm._LABEL_WIDTH_RATIO,
                cell_h * rm._LABEL_HEIGHT_RATIO,
            )
            for label, _ in tmpl["buttons"]
        ]
        # 統一字級 = 全體最小；最小者本身必能塞入（>0）。
        assert min(sizes) > 0


class TestDrawLabels:
    @_needs_font
    def test_template_image_differs_from_plain_solid(self):
        """有字型時，template 模式底圖應與純色 solid_png 不同（已印上文字）。"""
        tmpl = rm.TEMPLATES["booking4"]
        rgb = rm.THEMES["ocean_blue"]
        w = tmpl["size"]["width"]
        h = tmpl["size"]["height"]
        plain = rm.solid_png(w, h, rgb)
        _, labeled = rm.build_rich_menu_payload("booking4", "ocean_blue")
        assert labeled != plain
        # 尺寸不可變（LINE 對 rich menu 尺寸有硬性要求）。
        assert _png_dims(labeled) == (w, h)

    @_needs_font
    def test_all_templates_render_valid_png_with_labels(self):
        """每個版型都要能印字並輸出合法、尺寸正確的 PNG。"""
        for name, tmpl in rm.TEMPLATES.items():
            _, img = rm.build_rich_menu_payload(name, "line_green")
            assert _png_dims(img) == (
                tmpl["size"]["width"],
                tmpl["size"]["height"],
            )

    @_needs_font
    def test_vector_mode_also_labeled_and_differs_from_template(self):
        _, tmpl = rm.build_rich_menu_payload("booking4", "rose_pink", mode="template")
        _, vec = rm.build_rich_menu_payload("booking4", "rose_pink", mode="vector")
        assert vec[:8] == b"\x89PNG\r\n\x1a\n"
        assert vec != tmpl  # 底色不同 → 仍相異

    @_needs_font
    def test_labels_actually_present_via_pixel_diff(self):
        """逐像素比對：印字後非背景色的像素數應顯著 > 0。"""
        from PIL import Image

        rgb = rm.THEMES["dark"]
        _, labeled = rm.build_rich_menu_payload("grid1x1", "dark")
        img = Image.open(io.BytesIO(labeled)).convert("RGB")
        non_bg = sum(1 for px in img.getdata() if px != rgb)
        assert non_bg > 1000  # 文字筆畫像素

    def test_custom_image_not_labeled(self):
        """custom_image 模式維持原樣穿透——不可被印字改動。"""
        raw = b"\x89PNG\r\n\x1a\n custom-bytes-untouched"
        _, image = rm.build_rich_menu_payload(
            "booking3", "dark", mode="custom_image", image_bytes=raw
        )
        assert image == raw


class TestGeometryUnified:
    """畫字幾何必須與 tap areas 同一套——跨欄 bug(grid3x4x4)的直接防護。"""

    @pytest.mark.parametrize("name", list(rm.TEMPLATES.keys()))
    def test_cell_boxes_match_areas(self, name):
        """_cell_boxes 與 _areas 的 bounds 必須一一相等(含餘數補償)。"""
        tmpl = rm.TEMPLATES[name]
        boxes = rm._cell_boxes(tmpl)
        areas = rm._areas(tmpl)
        assert len(boxes) == len(areas)
        for (x, y, w, h), area in zip(boxes, areas):
            b = area["bounds"]
            assert (x, y, w, h) == (b["x"], b["y"], b["width"], b["height"])

    @_needs_font
    def test_grid3x4x4_label_positions_match_tap_areas(self, monkeypatch):
        """每個標籤的繪製座標必須落在自己按鈕的 tap bounds 內。

        舊實作以均勻 4x3 格畫字,第一列第 3 顆標籤畫在 x=1562(隸屬第 2 顆
        的 tap 區),此測試對舊碼必紅。
        """
        from PIL import ImageDraw

        calls: list[tuple[float, float]] = []
        orig = ImageDraw.ImageDraw.text

        def spy(self, xy, *args, **kwargs):
            calls.append(xy)
            return orig(self, xy, *args, **kwargs)

        monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
        rm.build_rich_menu_payload("grid3x4x4", "boutique")
        areas = rm._areas(rm.TEMPLATES["grid3x4x4"])
        assert len(calls) == len(areas)
        for (cx, cy), area in zip(calls, areas):
            b = area["bounds"]
            assert b["x"] <= cx < b["x"] + b["width"]
            assert b["y"] <= cy < b["y"] + b["height"]

    @_needs_font
    def test_fit_font_size_returns_zero_when_impossible(self):
        """連最小字級都塞不下時必須回 0(跳過畫字),不可硬畫溢出。"""
        from PIL import Image, ImageDraw

        draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        assert rm._fit_font_size(draw, "可預約時段", 5.0, 5.0) == 0

    @_needs_font
    def test_fit_font_size_accounts_for_stroke(self):
        """回傳字級以繪製時的描邊寬度量測仍須塞進留邊框。"""
        from PIL import Image, ImageDraw

        draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        max_w, max_h = 500.0, 260.0
        size = rm._fit_font_size(draw, "可預約時段", max_w, max_h)
        assert size > 0
        font = rm._load_font(size)
        sw = max(2, size // 18)
        x0, y0, x1, y1 = draw.textbbox((0, 0), "可預約時段", font=font, stroke_width=sw)
        assert (x1 - x0) <= max_w
        assert (y1 - y0) <= max_h


class TestCardRendering:
    """卡片式精品風渲染:漸層底、卡縫、brand 主題。"""

    def test_brand_theme_in_catalog(self):
        assert "brand" in rm.THEMES

    @_needs_font
    @pytest.mark.parametrize("name", list(rm.TEMPLATES.keys()))
    def test_card_render_smoke_all_templates(self, name):
        tmpl = rm.TEMPLATES[name]
        _, img = rm.build_rich_menu_payload(name, "brand")
        assert _png_dims(img) == (tmpl["size"]["width"], tmpl["size"]["height"])

    @_needs_font
    def test_gradient_background_present(self):
        """頂與底的背景像素應不同(上下漸層)。"""
        from PIL import Image

        _, png = rm.build_rich_menu_payload("booking4", "brand")
        img = Image.open(io.BytesIO(png)).convert("RGB")
        assert img.getpixel((3, 3)) != img.getpixel((3, img.height - 4))

    @_needs_font
    def test_card_gap_shows_background(self):
        """格框交界(卡縫)應露出漸層底而非卡面色。"""
        from PIL import Image

        _, png = rm.build_rich_menu_payload("booking4", "brand")
        img = Image.open(io.BytesIO(png)).convert("RGB")
        boxes = rm._cell_boxes(rm.TEMPLATES["booking4"])
        # 第 1、2 格交界的中線(x=1250 附近)是卡縫,不可是卡面暖白
        x0, y0, w, h = boxes[0]
        gap_px = img.getpixel((x0 + w, y0 + h // 2))
        card_px = img.getpixel((x0 + w // 2, y0 + h // 2))
        assert gap_px != card_px


class TestGracefulFallback:
    def test_no_font_returns_base_unchanged(self, monkeypatch):
        """無字型時 _draw_labels 必須原封回傳底圖，不丟例外。"""
        monkeypatch.setattr(rm, "_load_font", lambda size: None)
        base = rm.solid_png(300, 200, (10, 20, 30))
        assert rm._draw_labels(base, rm.TEMPLATES["booking4"]) == base

    def test_env_disable_skips_labeling(self, monkeypatch):
        """SAAS_RICH_MENU_LABELS=0 → template 底圖等於純色 solid_png。"""
        monkeypatch.setenv("SAAS_RICH_MENU_LABELS", "0")
        tmpl = rm.TEMPLATES["booking3"]
        rgb = rm.THEMES["sunset_orange"]
        plain = rm.solid_png(
            tmpl["size"]["width"], tmpl["size"]["height"], rgb
        )
        _, img = rm.build_rich_menu_payload("booking3", "sunset_orange")
        assert img == plain

    def test_draw_error_falls_back(self, monkeypatch):
        """繪圖過程拋例外時回退原圖（模擬 Pillow 內部失敗）。"""

        def _boom(size):
            raise RuntimeError("font blew up")

        monkeypatch.setattr(rm, "_load_font", _boom)
        base = rm.solid_png(300, 200, (1, 2, 3))
        # _draw_labels 內部對整段 try/except，應吞掉例外回傳原圖。
        assert rm._draw_labels(base, rm.TEMPLATES["booking3"]) == base
