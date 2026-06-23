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
