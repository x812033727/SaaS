"""新功能管理 UI（/ui/locations, /staff, /services, /campaigns, /flex-menu,
/portfolio, /profile, /pos, /faq）測試。

鏡像 tests/test_ui*.py：in-memory engine + register/login 取 cookie，再驅動頁面。
預設租戶所有進階功能皆開通（features_default_enabled=True），feature-locked 情境
以 /ui/features/{F}/unsubscribe 明確關閉。
"""

from __future__ import annotations

import os
import re
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
    FakeLinePushClient,
    FakeLineRichMenuClient,
    StubLineBotInfoClient,
    get_bot_info_client,
    get_push_client,
    get_rich_menu_client,
)
from saas_mvp.models.customer import Customer  # noqa: E402
from saas_mvp.models.product import Product  # noqa: E402
from saas_mvp.models.user import User  # noqa: E402

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
_app.dependency_overrides[get_push_client] = lambda: FakeLinePushClient()


@pytest.fixture()
def client():
    with TestClient(_app, raise_server_exceptions=True) as c:
        yield c


def _login(client) -> str:
    """註冊 + 登入；回傳 email（兼作識別）。"""
    email = f"u_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={
        "email": email, "password": "Test1234!", "tenant_name": f"t_{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 201, r.text
    r = client.post("/ui/login", data={"email": email, "password": "Test1234!"})
    assert r.status_code == 200
    return email


def _tenant_id_for(email: str) -> int:
    db = _Session()
    try:
        return db.query(User).filter(User.email == email).first().tenant_id
    finally:
        db.close()


def _disable(client, feature: str) -> None:
    r = client.post(f"/ui/features/{feature}/unsubscribe")
    assert r.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────


class TestLocationsUI:
    def test_page_renders(self, client):
        _login(client)
        r = client.get("/ui/locations")
        assert r.status_code == 200
        assert "分店" in r.text

    def test_locked_when_disabled(self, client):
        _login(client)
        _disable(client, "MULTI_LOCATION")
        r = client.get("/ui/locations")
        assert "尚未開通" in r.text

    def test_create_roundtrip(self, client):
        _login(client)
        r = client.post("/ui/locations", data={
            "name": "信義店", "address": "台北市", "phone": "0212345678",
        })
        assert r.status_code == 200
        assert "信義店" in r.text


class TestStaffUI:
    def test_page_renders(self, client):
        _login(client)
        r = client.get("/ui/staff")
        assert r.status_code == 200
        assert "員工" in r.text

    def test_locked_when_disabled(self, client):
        _login(client)
        _disable(client, "STAFF_SCHEDULING")
        assert "尚未開通" in client.get("/ui/staff").text

    def test_create_and_portal_link(self, client):
        _login(client)
        r = client.post("/ui/staff", data={
            "name": "小明", "role": "設計師", "location_id": "", "booking_mode": "capacity",
        })
        assert r.status_code == 200
        assert "小明" in r.text
        # 員工列表內含 staff id；產生連結後出現 /s/
        # 取得 staff id（第一個員工）
        r2 = client.post("/ui/staff/1/rotate-token")
        assert r2.status_code == 200
        assert "/s/" in r2.text


class TestServicesUI:
    def test_page_renders(self, client):
        _login(client)
        r = client.get("/ui/services")
        assert r.status_code == 200
        assert "服務項目" in r.text

    def test_locked_when_disabled(self, client):
        _login(client)
        _disable(client, "SERVICE_CATALOG")
        assert "尚未開通" in client.get("/ui/services").text

    def test_create_service_roundtrip(self, client):
        _login(client)
        r = client.post("/ui/services", data={
            "name": "洗剪護", "duration_minutes": "90", "price_cents": "60000",
            "category_id": "", "location_id": "",
        })
        assert r.status_code == 200
        assert "洗剪護" in r.text

    def test_edit_and_delete_service(self, client):
        _login(client)
        created = client.post("/ui/services", data={
            "name": "待改服務", "duration_minutes": "30", "price_cents": "10000",
            "category_id": "", "location_id": "",
        })
        assert "待改服務" in created.text
        sid = _last_service_id(client)
        # 編輯：改名 + 改價
        edited = client.post(f"/ui/services/{sid}/edit", data={
            "name": "已改服務", "duration_minutes": "45", "price_cents": "20000",
            "category_id": "", "location_id": "", "is_active": "on",
        })
        assert edited.status_code == 200
        assert "已改服務" in edited.text and "待改服務" not in edited.text
        # 刪除
        deleted = client.post(f"/ui/services/{sid}/delete")
        assert deleted.status_code == 200
        assert "已改服務" not in deleted.text

    def test_edit_and_delete_category(self, client):
        _login(client)
        client.post("/ui/services/categories", data={"name": "待刪分類", "sort_order": "0"})
        cid = _last_category_id(client)
        edited = client.post(f"/ui/services/categories/{cid}/edit",
                             data={"name": "已改分類", "sort_order": "3"})
        assert edited.status_code == 200
        assert "已改分類" in edited.text
        deleted = client.post(f"/ui/services/categories/{cid}/delete")
        assert deleted.status_code == 200
        assert "已改分類" not in deleted.text


def _last_service_id(client) -> int:
    html = client.get("/ui/services").text
    ids = [int(m) for m in re.findall(r"/ui/services/(\d+)/edit", html)]
    return max(ids)


def _last_category_id(client) -> int:
    html = client.get("/ui/services").text
    ids = [int(m) for m in re.findall(r"/ui/services/categories/(\d+)/edit", html)]
    return max(ids)


class TestCampaignsUI:
    def test_page_renders(self, client):
        _login(client)
        r = client.get("/ui/campaigns")
        assert r.status_code == 200
        assert "行銷活動" in r.text

    def test_locked_when_disabled(self, client):
        _login(client)
        _disable(client, "MARKETING_AUTO")
        assert "尚未開通" in client.get("/ui/campaigns").text

    def test_create_and_run(self, client):
        _login(client)
        r = client.post("/ui/campaigns", data={
            "name": "週年慶", "type": "broadcast", "message_template": "您好 {name}",
            "schedule_at": "", "segment_json": "", "reward_type": "", "reward_value": "",
        })
        assert r.status_code == 200
        assert "週年慶" in r.text
        # 立即執行（無顧客 → sent 0）
        r2 = client.post("/ui/campaigns/1/run")
        assert r2.status_code == 200
        assert "已執行" in r2.text


class TestFlexMenuUI:
    def test_page_renders(self, client):
        _login(client)
        r = client.get("/ui/flex-menu")
        assert r.status_code == 200
        assert "圖文選單" in r.text

    def test_locked_when_disabled(self, client):
        _login(client)
        _disable(client, "FLEX_MENU")
        assert "尚未開通" in client.get("/ui/flex-menu").text

    def test_add_card_and_preview(self, client):
        _login(client)
        r = client.post("/ui/flex-menu/cards", data={
            "title": "立即預約", "action_type": "uri", "action_data": "https://example.com",
            "subtitle": "馬上線上預約", "image_url": "", "bg_color": "",
        })
        assert r.status_code == 200
        assert "立即預約" in r.text
        assert "預覽" in r.text


class TestPortfolioUI:
    def test_page_renders(self, client):
        _login(client)
        r = client.get("/ui/portfolio")
        assert r.status_code == 200
        assert "作品集" in r.text

    def test_locked_when_disabled(self, client):
        _login(client)
        _disable(client, "PUBLIC_PROFILE")
        assert "尚未開通" in client.get("/ui/portfolio").text

    def test_add_item_roundtrip(self, client):
        _login(client)
        r = client.post("/ui/portfolio/items", data={
            "image_url": "https://img.example.com/a.jpg", "caption": "範例作品",
            "category_id": "", "sort_order": "0",
        })
        assert r.status_code == 200
        assert "範例作品" in r.text


class TestProfileUI:
    def test_page_renders(self, client):
        _login(client)
        r = client.get("/ui/profile")
        assert r.status_code == 200
        assert "店家頁" in r.text

    def test_locked_when_disabled(self, client):
        _login(client)
        _disable(client, "PUBLIC_PROFILE")
        assert "尚未開通" in client.get("/ui/profile").text

    def test_upsert_and_public_link(self, client):
        _login(client)
        slug = f"shop-{uuid.uuid4().hex[:6]}"
        r = client.post("/ui/profile", data={
            "slug": slug, "display_name": "我的店", "banner_url": "",
            "theme_color": "", "social_links": "", "seo_title": "", "seo_description": "",
            "intro": "歡迎光臨", "is_published": "true",
        })
        assert r.status_code == 200
        assert "已儲存" in r.text
        assert f"/p/{slug}" in r.text


class TestPosUI:
    def _seed(self, email: str) -> tuple[int, int]:
        """為該租戶建立一名顧客與一項商品；回 (customer_id, product_id)。"""
        tid = _tenant_id_for(email)
        db = _Session()
        try:
            cust = Customer(
                tenant_id=tid, line_user_id="U" + uuid.uuid4().hex,
                display_name="王小姐", phone="0987654321",
            )
            prod = Product(tenant_id=tid, name="洗髮精", price_cents=30000, stock=10)
            db.add(cust)
            db.add(prod)
            db.commit()
            return cust.id, prod.id
        finally:
            db.close()

    def test_page_renders(self, client):
        _login(client)
        r = client.get("/ui/pos")
        assert r.status_code == 200
        assert "POS 結帳" in r.text

    def test_locked_when_disabled(self, client):
        _login(client)
        _disable(client, "PRODUCT_SALES")
        assert "尚未開通" in client.get("/ui/pos").text

    def test_lookup_and_checkout(self, client):
        email = _login(client)
        cust_id, prod_id = self._seed(email)
        # 查會員
        r = client.post("/ui/pos/lookup", data={"phone": "0987654321"})
        assert r.status_code == 200
        assert "王小姐" in r.text
        # 結帳（買 1 件洗髮精）
        r2 = client.post("/ui/pos/checkout", data={
            "customer_id": str(cust_id), "phone": "0987654321",
            f"qty_{prod_id}": "1", "coupon_code": "", "points_to_redeem": "0",
        })
        assert r2.status_code == 200
        assert "結帳完成" in r2.text

    def test_checkout_creates_order(self, client):
        email = _login(client)
        cust_id, prod_id = self._seed(email)
        tid = _tenant_id_for(email)
        client.post("/ui/pos/checkout", data={
            "customer_id": str(cust_id), "phone": "0987654321",
            f"qty_{prod_id}": "2", "coupon_code": "", "points_to_redeem": "0",
        })
        from saas_mvp.models.order import Order
        db = _Session()
        try:
            orders = db.query(Order).filter(Order.tenant_id == tid).all()
            assert len(orders) == 1
            assert orders[0].total_cents == 60000
        finally:
            db.close()


class TestFaqUI:
    def test_page_renders(self, client):
        _login(client)
        r = client.get("/ui/faq")
        assert r.status_code == 200
        assert "AI 客服" in r.text

    def test_locked_when_disabled(self, client):
        _login(client)
        _disable(client, "AI_ASSISTANT")
        assert "尚未開通" in client.get("/ui/faq").text

    def test_create_faq_and_ask(self, client):
        _login(client)
        r = client.post("/ui/faq", data={
            "question": "營業時間？", "answer": "每天 10-22 點", "sort_order": "0",
        })
        assert r.status_code == 200
        assert "營業時間" in r.text
        # AI 測試（stub：context 命中即回 FAQ）
        r2 = client.post("/ui/faq/ask", data={"question": "營業時間？"})
        assert r2.status_code == 200
        assert "回答" in r2.text


class TestDashboardPushPanel:
    def test_push_usage_panel(self, client):
        _login(client)
        r = client.get("/ui/")
        assert r.status_code == 200
        assert "本月推播用量" in r.text


class TestTenantIsolation:
    def test_locations_isolated(self, client):
        # 租戶 A 建分店
        client.post("/ui/login", data={})  # noop
        _login(client)
        client.post("/ui/locations", data={"name": "A獨有分店", "address": "", "phone": ""})
        a_page = client.get("/ui/locations")
        assert "A獨有分店" in a_page.text
        # 換成租戶 B（重新註冊登入覆寫 cookie）
        _login(client)
        b_page = client.get("/ui/locations")
        assert "A獨有分店" not in b_page.text

    def test_pos_lookup_isolated(self, client):
        email_a = _login(client)
        TestPosUI()._seed(email_a)
        # B 租戶查同電話 → 查無
        _login(client)
        r = client.post("/ui/pos/lookup", data={"phone": "0987654321"})
        assert r.status_code == 200
        assert "王小姐" not in r.text
        assert "查無此電話" in r.text
