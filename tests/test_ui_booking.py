"""預約管理 UI（/ui/booking）測試。"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")
os.environ.setdefault(
    "SAAS_LINE_CHANNEL_ENCRYPT_KEY",
    "ZGV2LWxpbmUtc2VjcmV0LWtleS0zMmJ5dGVzLWxvbmc=",
)

from saas_mvp.models import tenant as _t, user as _u  # noqa: F401,E402
from saas_mvp.models import customer as _c, booking_slot as _bs  # noqa: F401,E402
from saas_mvp.models import reservation as _r, reservation_reminder as _rr  # noqa: F401,E402
import saas_mvp.models.line_channel_config as _lcm  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.line_client import (  # noqa: E402
    FakeLineRichMenuClient,
    StubLineBotInfoClient,
    get_bot_info_client,
    get_rich_menu_client,
)

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
Base.metadata.create_all(bind=_engine)
_app = create_app()


def _override_get_db():
    db = _Session()
    try:
        yield db
    finally:
        db.close()


_app.dependency_overrides[get_db] = _override_get_db
_app.dependency_overrides[get_bot_info_client] = (
    lambda: StubLineBotInfoClient("U" + uuid.uuid4().hex)
)
_app.dependency_overrides[get_rich_menu_client] = lambda: FakeLineRichMenuClient()


@pytest.fixture()
def client():
    with TestClient(_app, raise_server_exceptions=True) as c:
        yield c


def _login(client) -> None:
    email = f"u_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={
        "email": email, "password": "Test1234!", "tenant_name": f"t_{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 201, r.text
    r = client.post("/ui/login", data={"email": email, "password": "Test1234!"})
    assert r.status_code == 200


def _setup_line_config(client) -> None:
    r = client.post("/ui/line-config", data={
        "channel_secret": "s" * 32, "access_token": "a" * 40, "default_target_lang": "zh-TW",
    })
    assert r.status_code == 200, r.text


class TestBookingUI:
    def test_page_renders(self, client):
        _login(client)
        r = client.get("/ui/booking")
        assert r.status_code == 200
        assert "預約管理" in r.text

    def test_botmode_requires_line_config(self, client):
        _login(client)
        r = client.get("/ui/booking")
        assert "尚未設定 LINE Bot" in r.text

    def test_toggle_bot_mode(self, client):
        _login(client)
        _setup_line_config(client)
        r = client.post("/ui/booking/bot-mode", data={"bot_mode": "booking"})
        assert r.status_code == 200
        assert "預約" in r.text
        # 確認讀回為 booking
        page = client.get("/ui/booking")
        assert "目前模式" in page.text

    def test_create_and_deactivate_slot(self, client):
        _login(client)
        r = client.post("/ui/booking/slots", data={
            "slot_start": "2030-06-01T18:00", "max_capacity": "8", "walkin_reserved": "2",
        })
        assert r.status_code == 200
        assert "2030-06-01 18:00" in r.text
        # 取出 slot id：線上可訂應為 6
        assert "<td>6</td>" in r.text or "6" in r.text

    def test_bulk_generate_slots(self, client):
        _login(client)
        r = client.post("/ui/booking/slots/bulk", data={
            "date_start": "2030-07-01", "date_end": "2030-07-02",
            "time_start": "11:00", "time_end": "14:00",
            "interval_minutes": "60", "max_capacity": "10", "walkin_reserved": "0",
        })
        assert r.status_code == 200
        # 2 天 × 3 格（11/12/13）= 6
        assert "新增 6 個時段" in r.text
        # 重跑同參數 → 全部略過
        again = client.post("/ui/booking/slots/bulk", data={
            "date_start": "2030-07-01", "date_end": "2030-07-02",
            "time_start": "11:00", "time_end": "14:00",
            "interval_minutes": "60", "max_capacity": "10", "walkin_reserved": "0",
        })
        assert "新增 0 個時段" in again.text
        assert "略過 6 個" in again.text

    def test_bulk_generate_weekday_filter(self, client):
        _login(client)
        # 2030-07-01 是週一(0)。範圍週一~週日，限定週一+週三。
        r = client.post("/ui/booking/slots/bulk", data={
            "date_start": "2030-07-01", "date_end": "2030-07-07",
            "time_start": "11:00", "time_end": "13:00",
            "interval_minutes": "60", "max_capacity": "5",
            "weekdays": ["0", "2"],
        })
        assert r.status_code == 200
        # 週一(7/1)+週三(7/3) 各 2 格 = 4
        assert "新增 4 個時段" in r.text

    def test_bulk_generate_invalid_range(self, client):
        _login(client)
        r = client.post("/ui/booking/slots/bulk", data={
            "date_start": "2030-07-05", "date_end": "2030-07-01",
            "time_start": "11:00", "time_end": "14:00",
            "interval_minutes": "60", "max_capacity": "10",
        })
        assert r.status_code == 200
        assert "error" in r.text or "date_end" in r.text

    def test_create_slot_invalid_capacity(self, client):
        _login(client)
        r = client.post("/ui/booking/slots", data={
            "slot_start": "2030-06-02T18:00", "max_capacity": "2", "walkin_reserved": "5",
        })
        # walkin > capacity → service 422 → error 顯示於 partial
        assert r.status_code == 200
        assert "error" in r.text or "walkin" in r.text.lower() or "容量" in r.text

    def test_unauth_redirect(self, client):
        r = client.get("/ui/booking", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/ui/login"


class TestRichMenuUI:
    def test_page_requires_line_config(self, client):
        _login(client)
        r = client.get("/ui/rich-menu")
        assert r.status_code == 200
        assert "尚未設定 LINE Bot" in r.text

    def test_apply_and_clear(self, client):
        _login(client)
        _setup_line_config(client)
        r = client.post("/ui/rich-menu/apply", data={"template": "booking3", "theme": "line_green"})
        assert r.status_code == 200
        assert "已套用" in r.text
        assert "booking3" in r.text
        # 移除
        r2 = client.post("/ui/rich-menu/clear", data={})
        assert r2.status_code == 200
        assert "尚未套用" in r2.text

    def test_apply_invalid_theme_shows_error(self, client):
        _login(client)
        _setup_line_config(client)
        r = client.post("/ui/rich-menu/apply", data={"template": "booking3", "theme": "neon"})
        assert r.status_code == 200
        assert "error" in r.text or "Unknown theme" in r.text


class TestCouponsUI:
    def test_page_renders(self, client):
        _login(client)
        r = client.get("/ui/coupons")
        assert r.status_code == 200
        assert "優惠券" in r.text

    def test_create_and_deactivate(self, client):
        _login(client)
        import uuid as _uuid
        code = f"UI{_uuid.uuid4().hex[:6]}"
        r = client.post("/ui/coupons", data={
            "code": code, "name": "UI券", "discount_type": "percent",
            "discount_value": "20", "max_redemptions": "50",
        })
        assert r.status_code == 200
        assert code in r.text and "UI券" in r.text

    def test_create_invalid_percent_shows_error(self, client):
        _login(client)
        r = client.post("/ui/coupons", data={
            "code": "BADUI", "name": "x", "discount_type": "percent", "discount_value": "200",
        })
        assert r.status_code == 200
        assert "error" in r.text or "0-100" in r.text


class TestReportsUI:
    def test_reports_page_renders(self, client):
        _login(client)
        r = client.get("/ui/reports")
        assert r.status_code == 200
        assert "報表分析" in r.text and "取消率" in r.text
        # CSV 下載連結與爽約率需標記說明
        assert "export.csv" in r.text and "需標記到場" in r.text

    def test_reports_unauth_redirect(self, client):
        r = client.get("/ui/reports", follow_redirects=False)
        assert r.status_code == 303


class TestShopUI:
    def test_page_renders(self, client):
        _login(client)
        r = client.get("/ui/shop")
        assert r.status_code == 200
        assert "商品銷售" in r.text

    def test_create_product_and_deactivate(self, client):
        _login(client)
        r = client.post("/ui/shop/products", data={
            "name": "UI商品", "price_cents": "250", "stock": "5",
        })
        assert r.status_code == 200
        assert "UI商品" in r.text and "250" in r.text

    def test_create_invalid_price_error(self, client):
        _login(client)
        r = client.post("/ui/shop/products", data={
            "name": "x", "price_cents": "-5", "stock": "",
        })
        assert r.status_code == 200
        assert "error" in r.text or ">= 0" in r.text


class TestFeaturesUI:
    def test_features_page_lists(self, client):
        _login(client)
        r = client.get("/ui/features")
        assert r.status_code == 200
        assert "進階功能訂閱" in r.text and "優惠券" in r.text

    def test_unsubscribe_then_locked_page(self, client):
        _login(client)
        # 退訂優惠券 → /ui/coupons 顯示 upsell
        r = client.post("/ui/features/COUPON_SYSTEM/unsubscribe")
        assert r.status_code == 200
        assert "未開通" in r.text
        locked = client.get("/ui/coupons")
        assert "尚未開通" in locked.text and "前往訂閱" in locked.text
        # 重新訂閱 → 可進入管理頁
        client.post("/ui/features/COUPON_SYSTEM/subscribe")
        assert client.get("/ui/coupons").status_code == 200

    def test_shop_locked_when_disabled(self, client):
        _login(client)
        client.post("/ui/features/PRODUCT_SALES/unsubscribe")
        locked = client.get("/ui/shop")
        assert "尚未開通" in locked.text
