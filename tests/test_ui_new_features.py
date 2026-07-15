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

    def _create_staff(self, client, name: str) -> int:
        email = _login(client)
        client.post("/ui/staff", data={
            "name": name, "role": "", "location_id": "", "booking_mode": "capacity",
        })
        db = _Session()
        try:
            from saas_mvp.models.staff import Staff
            tid = _tenant_id_for(email)
            return (
                db.query(Staff)
                .filter(Staff.tenant_id == tid, Staff.name == name)
                .first()
                .id
            )
        finally:
            db.close()

    def test_shift_edit_and_update(self, client):
        sid = self._create_staff(client, "班表哥")
        client.post(f"/ui/staff/{sid}/shifts", data={
            "weekday": "0", "start_time": "09:00", "end_time": "12:00", "rotation": "day",
        })
        db = _Session()
        try:
            from saas_mvp.models.staff_shift import StaffShift
            shid = (
                db.query(StaffShift).filter(StaffShift.staff_id == sid).first().id
            )
        finally:
            db.close()
        # 編輯表單預填
        r = client.get(f"/ui/staff/{sid}/shifts/{shid}/edit")
        assert r.status_code == 200
        assert f"/ui/staff/{sid}/shifts/{shid}/update" in r.text
        assert 'value="09:00"' in r.text
        # 更新（改時間 + 改為每日）
        r = client.post(f"/ui/staff/{sid}/shifts/{shid}/update", data={
            "weekday": "", "start_time": "10:00", "end_time": "15:00", "rotation": "night",
        })
        assert r.status_code == 200
        assert "10:00 - 15:00" in r.text and "每日" in r.text
        assert f"/shifts/{shid}/update" not in r.text  # 編輯列已收合
        # 撞唯一約束 (staff, weekday, start_time) → 409 錯誤顯示且停在編輯列
        client.post(f"/ui/staff/{sid}/shifts", data={
            "weekday": "3", "start_time": "10:00", "end_time": "15:00", "rotation": "",
        })
        r = client.post(f"/ui/staff/{sid}/shifts/{shid}/update", data={
            "weekday": "3", "start_time": "10:00", "end_time": "15:00", "rotation": "",
        })
        assert r.status_code == 200
        assert "already exists" in r.text
        assert f"/ui/staff/{sid}/shifts/{shid}/update" in r.text

    def test_leave_edit_and_update(self, client):
        sid = self._create_staff(client, "請假姐")
        client.post(f"/ui/staff/{sid}/leaves", data={
            "start_at": "2031-03-01T09:00", "end_at": "2031-03-02T09:00", "reason": "出國",
        })
        db = _Session()
        try:
            from saas_mvp.models.staff_leave import StaffLeave
            lvid = (
                db.query(StaffLeave).filter(StaffLeave.staff_id == sid).first().id
            )
        finally:
            db.close()
        # 編輯表單預填
        r = client.get(f"/ui/staff/{sid}/leaves/{lvid}/edit")
        assert r.status_code == 200
        assert 'value="2031-03-01T09:00"' in r.text and 'value="出國"' in r.text
        # 更新
        r = client.post(f"/ui/staff/{sid}/leaves/{lvid}/update", data={
            "start_at": "2031-03-01T09:00", "end_at": "2031-03-03T18:00", "reason": "改期",
        })
        assert r.status_code == 200
        assert "2031-03-03 18:00" in r.text and "改期" in r.text
        # 顛倒區間 → 422 錯誤顯示且停在編輯列
        r = client.post(f"/ui/staff/{sid}/leaves/{lvid}/update", data={
            "start_at": "2031-03-05T09:00", "end_at": "2031-03-04T09:00", "reason": "",
        })
        assert r.status_code == 200
        assert "end_at must be after start_at" in r.text
        assert f"/ui/staff/{sid}/leaves/{lvid}/update" in r.text

    def test_staff_list_partial_for_cancel(self, client):
        self._create_staff(client, "取消哥")
        r = client.get("/ui/staff/list")
        assert r.status_code == 200
        assert "取消哥" in r.text
        assert "/shifts/" not in r.text or "/shifts/bulk" in r.text  # 無展開的編輯列


class TestCustomersUI:
    def _seed_customer(self, email: str, name: str = "王小姐") -> int:
        import uuid as _uuid

        from saas_mvp.models.customer import Customer

        db = _Session()
        try:
            c = Customer(
                tenant_id=_tenant_id_for(email),
                line_user_id=f"U{_uuid.uuid4().hex[:12]}",
                display_name=name,
            )
            db.add(c)
            db.commit()
            return c.id
        finally:
            db.close()

    def test_page_renders(self, client):
        # upstream 整併：GET /ui/customers 由 CRM 清單頁接手（含「標籤管理」
        # 入口按鈕）；本區段的管理檢視移至 GET /ui/customers/list。
        _login(client)
        r = client.get("/ui/customers")
        assert r.status_code == 200
        assert "顧客清單" in r.text and "標籤管理" in r.text
        r2 = client.get("/ui/customers/list")
        assert r2.status_code == 200
        assert "顧客管理" in r2.text

    def test_edit_phone_note(self, client):
        email = _login(client)
        cid = self._seed_customer(email)
        r = client.get(f"/ui/customers/{cid}/edit")
        assert r.status_code == 200
        assert f"/ui/customers/{cid}/update" in r.text
        r = client.post(f"/ui/customers/{cid}/update", data={
            "phone": "0912345678", "note": "VIP 常客",
        })
        assert r.status_code == 200
        assert "0912345678" in r.text and "VIP 常客" in r.text

    def test_tag_crud_attach_detach(self, client):
        email = _login(client)
        cid = self._seed_customer(email, name="標籤客")
        # 建標籤
        r = client.post("/ui/customers/tags", data={"name": "熟客", "color": "#00aa00"})
        assert r.status_code == 200 and "熟客" in r.text
        db = _Session()
        try:
            from saas_mvp.models.customer_tag import CustomerTag
            tag_id = (
                db.query(CustomerTag)
                .filter(CustomerTag.tenant_id == _tenant_id_for(email))
                .first()
                .id
            )
        finally:
            db.close()
        # 掛上
        r = client.post(f"/ui/customers/{cid}/tags/attach", data={"tag_id": str(tag_id)})
        assert r.status_code == 200
        assert f"/ui/customers/{cid}/tags/{tag_id}/detach" in r.text
        # 卸下
        r = client.post(f"/ui/customers/{cid}/tags/{tag_id}/detach")
        assert r.status_code == 200
        assert f"/ui/customers/{cid}/tags/{tag_id}/detach" not in r.text
        # 改名
        r = client.post(f"/ui/customers/tags/{tag_id}/update", data={
            "name": "超級熟客", "color": "",
        })
        assert "超級熟客" in r.text
        # 刪標籤
        r = client.post(f"/ui/customers/tags/{tag_id}/delete")
        assert "超級熟客" not in r.text

    def test_delete_customer(self, client):
        email = _login(client)
        cid = self._seed_customer(email, name="要刪除的客")
        r = client.post(f"/ui/customers/{cid}/delete")
        assert r.status_code == 200
        assert "要刪除的客" not in r.text


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

    def test_create_with_segment_selector_builds_json(self, client):
        """表單選擇器（tier/min_bookings）組出 segment_json，列表顯示人話 chips。"""
        import json as _json

        from saas_mvp.models.campaign import Campaign

        _login(client)
        r = client.post("/ui/campaigns", data={
            "name": "分眾活動", "type": "broadcast",
            "message_template": "hi {name}",
            "schedule_at": "", "segment_json": "",
            "segment_tier": "gold", "segment_min_bookings": "3",
            "segment_location_id": "",
            "reward_type": "", "reward_value": "",
        })
        assert r.status_code == 200
        assert "分眾活動" in r.text
        assert "等級：gold" in r.text  # 人話 chips
        assert "預約 ≥ 3 次" in r.text
        db = _Session()
        try:
            camp = db.query(Campaign).filter(
                Campaign.name == "分眾活動"
            ).order_by(Campaign.id.desc()).first()
            seg = _json.loads(camp.segment_json)
            assert seg == {"tier": "gold", "min_bookings": 3}
        finally:
            db.close()

    def test_raw_json_takes_precedence(self, client):
        import json as _json

        from saas_mvp.models.campaign import Campaign

        _login(client)
        r = client.post("/ui/campaigns", data={
            "name": "原始JSON活動", "type": "broadcast",
            "message_template": "hi",
            "schedule_at": "",
            "segment_json": '{"tier": "silver"}',
            "segment_tier": "gold",  # 應被原始 JSON 蓋過
            "segment_min_bookings": "", "segment_location_id": "",
            "reward_type": "", "reward_value": "",
        })
        assert r.status_code == 200
        db = _Session()
        try:
            camp = db.query(Campaign).filter(
                Campaign.name == "原始JSON活動"
            ).order_by(Campaign.id.desc()).first()
            assert _json.loads(camp.segment_json) == {"tier": "silver"}
        finally:
            db.close()

    def test_empty_selector_leaves_segment_null(self, client):
        from saas_mvp.models.campaign import Campaign

        _login(client)
        client.post("/ui/campaigns", data={
            "name": "全客群活動", "type": "broadcast",
            "message_template": "hi",
            "schedule_at": "", "segment_json": "",
            "segment_tier": "", "segment_min_bookings": "",
            "segment_location_id": "",
            "reward_type": "", "reward_value": "",
        })
        db = _Session()
        try:
            camp = db.query(Campaign).filter(
                Campaign.name == "全客群活動"
            ).order_by(Campaign.id.desc()).first()
            assert camp.segment_json is None
        finally:
            db.close()


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

    def test_delete_menu_resets_to_empty(self, client):
        email = _login(client)
        client.post("/ui/flex-menu/title", data={"title": "要刪的選單"})
        client.post("/ui/flex-menu/cards", data={
            "title": "卡片A", "action_type": "uri", "action_data": "https://example.com",
            "subtitle": "", "image_url": "", "bg_color": "",
        })
        r = client.post("/ui/flex-menu/delete")
        assert r.status_code == 200
        # 重設為空選單：卡片與標題都沒了
        assert "卡片A" not in r.text
        assert "要刪的選單" not in r.text
        assert "尚無卡片" in r.text
        # 卡片列不留孤兒
        db = _Session()
        try:
            from saas_mvp.models.flex_menu_card import FlexMenuCard
            tid = _tenant_id_for(email)
            assert (
                db.query(FlexMenuCard)
                .filter(FlexMenuCard.tenant_id == tid)
                .count()
                == 0
            )
        finally:
            db.close()


class TestNotesUI:
    def test_page_and_crud_roundtrip(self, client):
        _login(client)
        r = client.get("/ui/notes")
        assert r.status_code == 200 and "備註" in r.text
        # 新增
        r = client.post("/ui/notes", data={"title": "採購清單", "content": "衛生紙"})
        assert "採購清單" in r.text
        db = _Session()
        try:
            from saas_mvp.models.note import Note
            nid = db.query(Note).filter(Note.title == "採購清單").first().id
        finally:
            db.close()
        # 編輯
        r = client.get(f"/ui/notes/{nid}/edit")
        assert 'value="採購清單"' in r.text
        r = client.post(f"/ui/notes/{nid}/update", data={
            "title": "採購清單v2", "content": "衛生紙、洗手乳",
        })
        assert "採購清單v2" in r.text and "洗手乳" in r.text
        # 刪除
        r = client.post(f"/ui/notes/{nid}/delete")
        assert "採購清單v2" not in r.text


class TestApiKeysUI:
    def test_page_renders(self, client):
        _login(client)
        r = client.get("/ui/api-keys")
        assert r.status_code == 200
        assert "API 金鑰" in r.text

    def test_create_shows_plain_key_once_then_revoke(self, client):
        _login(client)
        r = client.post("/ui/api-keys", data={"name": "POS 串接"})
        assert r.status_code == 200
        assert "僅顯示這一次" in r.text
        # 明文 key 出現在建立回應
        import re
        m = re.search(r"myapp_[A-Za-z0-9_\-]{20,}", r.text)
        assert m, "建立回應應包含明文 key"
        plain = m.group(0)
        # 重新載入頁面 → 明文不再出現，只有前綴
        page = client.get("/ui/api-keys")
        assert plain not in page.text
        assert "POS 串接" in page.text
        # 撤銷
        db = _Session()
        try:
            from saas_mvp.models.api_key import ApiKey, hash_api_key
            row = (
                db.query(ApiKey)
                .filter(ApiKey.key_hash == hash_api_key(plain))
                .first()
            )
            key_id = row.id
        finally:
            db.close()
        r = client.post(f"/ui/api-keys/{key_id}/revoke")
        assert "已撤銷" in r.text


class TestAutoReplyUI:
    def test_page_renders(self, client):
        _login(client)
        r = client.get("/ui/auto-reply")
        assert r.status_code == 200
        assert "自動回覆規則" in r.text

    def test_create_edit_toggle_delete(self, client):
        email = _login(client)
        # 建立文字規則
        r = client.post("/ui/auto-reply", data={
            "keyword": "營業時間", "match_type": "exact", "reply_type": "text",
            "reply_text": "10:00-22:00", "flex_menu_id": "", "priority": "5",
        })
        assert r.status_code == 200
        assert "營業時間" in r.text and "10:00-22:00" in r.text
        db = _Session()
        try:
            from saas_mvp.models.auto_reply_rule import AutoReplyRule
            rid = (
                db.query(AutoReplyRule)
                .filter(AutoReplyRule.tenant_id == _tenant_id_for(email))
                .first()
                .id
            )
        finally:
            db.close()
        # 編輯表單預填
        r = client.get(f"/ui/auto-reply/{rid}/edit")
        assert 'value="營業時間"' in r.text
        # 更新
        r = client.post(f"/ui/auto-reply/{rid}/update", data={
            "keyword": "營業", "match_type": "prefix", "reply_type": "text",
            "reply_text": "平日 10:00-22:00", "flex_menu_id": "", "priority": "1",
        })
        assert "平日 10:00-22:00" in r.text and "開頭" in r.text
        # 停用/啟用 toggle
        r = client.post(f"/ui/auto-reply/{rid}/toggle")
        assert "badge off" in r.text
        r = client.post(f"/ui/auto-reply/{rid}/toggle")
        assert "badge on" in r.text
        # 刪除
        r = client.post(f"/ui/auto-reply/{rid}/delete")
        assert "營業" not in r.text or "尚無規則" in r.text

    def test_create_text_rule_without_text_shows_error(self, client):
        _login(client)
        r = client.post("/ui/auto-reply", data={
            "keyword": "測試", "match_type": "contains", "reply_type": "text",
            "reply_text": "", "flex_menu_id": "", "priority": "0",
        })
        assert r.status_code == 200
        assert "reply_text is required" in r.text


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

    def _create_faq(self, client, email):
        client.post("/ui/faq", data={
            "question": "可以刷卡嗎？", "answer": "可以，接受信用卡。", "sort_order": "0",
        })
        from saas_mvp.models.faq_entry import FAQEntry
        db = _Session()
        try:
            tid = _tenant_id_for(email)
            return db.query(FAQEntry).filter(FAQEntry.tenant_id == tid).first().id
        finally:
            db.close()

    def test_toggle_active(self, client):
        email = _login(client)
        fid = self._create_faq(client, email)
        # 新建預設啟用 → 停用
        r = client.post(f"/ui/faq/{fid}/toggle")
        assert r.status_code == 200
        from saas_mvp.models.faq_entry import FAQEntry
        db = _Session()
        try:
            assert db.get(FAQEntry, fid).is_active is False
        finally:
            db.close()
        # 再切回啟用
        client.post(f"/ui/faq/{fid}/toggle")
        db = _Session()
        try:
            assert db.get(FAQEntry, fid).is_active is True
        finally:
            db.close()

    def test_edit_form_renders_prefilled(self, client):
        email = _login(client)
        fid = self._create_faq(client, email)
        r = client.get(f"/ui/faq/{fid}/edit")
        assert r.status_code == 200
        assert f"/ui/faq/{fid}/update" in r.text
        assert "可以刷卡嗎？" in r.text  # 預填現有問題
        assert 'hx-get="/ui/faq/list"' in r.text

        # 取消只回 FAQ partial，不可把完整 HTML 頁面塞進 #faq-card。
        cancelled = client.get("/ui/faq/list")
        assert cancelled.status_code == 200
        assert "可以刷卡嗎？" in cancelled.text
        assert "<!doctype html>" not in cancelled.text.lower()
        assert "<h1>AI 客服</h1>" not in cancelled.text

    def test_update_content(self, client):
        email = _login(client)
        fid = self._create_faq(client, email)
        r = client.post(f"/ui/faq/{fid}/update", data={
            "question": "可以用 LINE Pay 嗎？",
            "answer": "可以，支援 LINE Pay 與行動支付。",
            "sort_order": "5",
        })
        assert r.status_code == 200
        assert "LINE Pay" in r.text
        from saas_mvp.models.faq_entry import FAQEntry
        db = _Session()
        try:
            f = db.get(FAQEntry, fid)
            assert f.question == "可以用 LINE Pay 嗎？"
            assert f.sort_order == 5
        finally:
            db.close()


class TestCouponCrud:
    def _create(self, client, email, code="SAVE10"):
        client.post("/ui/coupons", data={
            "code": code, "name": "舊名稱", "discount_type": "percent",
            "discount_value": "10", "max_redemptions": "5",
        })
        from saas_mvp.models.coupon import Coupon
        db = _Session()
        try:
            tid = _tenant_id_for(email)
            return db.query(Coupon).filter(
                Coupon.tenant_id == tid, Coupon.code == code
            ).first().id
        finally:
            db.close()

    def test_edit_form_and_update(self, client):
        email = _login(client)
        cid = self._create(client, email)
        r = client.get(f"/ui/coupons/{cid}/edit")
        assert r.status_code == 200
        assert f"/ui/coupons/{cid}/update" in r.text and "舊名稱" in r.text
        r2 = client.post(f"/ui/coupons/{cid}/update",
                         data={"name": "新名稱", "max_redemptions": "20"})
        assert r2.status_code == 200 and "新名稱" in r2.text
        from saas_mvp.models.coupon import Coupon
        db = _Session()
        try:
            c = db.get(Coupon, cid)
            assert c.name == "新名稱" and c.max_redemptions == 20
        finally:
            db.close()

    def test_delete_happy(self, client):
        email = _login(client)
        cid = self._create(client, email, code="DELME")
        assert client.post(f"/ui/coupons/{cid}/delete").status_code == 200
        from saas_mvp.models.coupon import Coupon
        db = _Session()
        try:
            assert db.get(Coupon, cid) is None
        finally:
            db.close()

    def test_delete_blocked_when_redeemed(self, client):
        email = _login(client)
        cid = self._create(client, email, code="USED")
        tid = _tenant_id_for(email)
        from saas_mvp.services import coupons as coupons_svc
        db = _Session()
        try:
            coupons_svc.redeem_coupon(db, tenant_id=tid, code="USED", line_user_id="Uabc")
        finally:
            db.close()
        r = client.post(f"/ui/coupons/{cid}/delete")
        assert r.status_code == 200 and "兌換紀錄" in r.text  # 被擋下並提示
        from saas_mvp.models.coupon import Coupon
        db = _Session()
        try:
            assert db.get(Coupon, cid) is not None  # 仍保留
        finally:
            db.close()


class TestShopProductCrud:
    def _create(self, client, email, name="舊商品"):
        client.post("/ui/shop/products", data={"name": name, "price_cents": "500", "stock": ""})
        from saas_mvp.models.product import Product
        db = _Session()
        try:
            tid = _tenant_id_for(email)
            return db.query(Product).filter(
                Product.tenant_id == tid, Product.name == name
            ).first().id
        finally:
            db.close()

    def test_edit_and_update(self, client):
        email = _login(client)
        pid = self._create(client, email)
        r = client.get(f"/ui/shop/products/{pid}/edit")
        assert r.status_code == 200 and "舊商品" in r.text
        r2 = client.post(f"/ui/shop/products/{pid}/update",
                         data={"name": "新商品", "price_cents": "800", "stock": "3"})
        assert "新商品" in r2.text
        from saas_mvp.models.product import Product
        db = _Session()
        try:
            p = db.get(Product, pid)
            assert p.name == "新商品" and p.price_cents == 800 and p.stock == 3
        finally:
            db.close()

    def test_delete_happy(self, client):
        email = _login(client)
        pid = self._create(client, email, "刪商品")
        assert client.post(f"/ui/shop/products/{pid}/delete").status_code == 200
        from saas_mvp.models.product import Product
        db = _Session()
        try:
            assert db.get(Product, pid) is None
        finally:
            db.close()

    def test_delete_blocked_when_ordered(self, client):
        email = _login(client)
        pid = self._create(client, email, "已售商品")
        tid = _tenant_id_for(email)
        from saas_mvp.services import shop as shop_svc
        db = _Session()
        try:
            shop_svc.create_order(db, tenant_id=tid, items=[(pid, 1)], line_user_id="Ubuy")
        finally:
            db.close()
        r = client.post(f"/ui/shop/products/{pid}/delete")
        assert r.status_code == 200 and "訂單紀錄" in r.text
        from saas_mvp.models.product import Product
        db = _Session()
        try:
            assert db.get(Product, pid) is not None
        finally:
            db.close()


class TestCampaignCrud:
    def _create(self, client, email, name="舊活動"):
        client.post("/ui/campaigns", data={
            "name": name, "type": "broadcast", "message_template": "嗨 {name}",
            "schedule_at": "", "segment_json": "", "reward_type": "", "reward_value": "",
        })
        from saas_mvp.models.campaign import Campaign
        db = _Session()
        try:
            tid = _tenant_id_for(email)
            return db.query(Campaign).filter(
                Campaign.tenant_id == tid, Campaign.name == name
            ).first().id
        finally:
            db.close()

    def test_edit_and_update(self, client):
        email = _login(client)
        cid = self._create(client, email)
        r = client.get(f"/ui/campaigns/{cid}/edit")
        assert r.status_code == 200 and "舊活動" in r.text
        r2 = client.post(f"/ui/campaigns/{cid}/update",
                         data={"name": "新活動", "message_template": "新訊息 {name}"})
        assert "新活動" in r2.text
        from saas_mvp.models.campaign import Campaign
        db = _Session()
        try:
            assert db.get(Campaign, cid).name == "新活動"
        finally:
            db.close()

    def test_delete_happy(self, client):
        email = _login(client)
        cid = self._create(client, email, "刪活動")
        assert client.post(f"/ui/campaigns/{cid}/delete").status_code == 200
        from saas_mvp.models.campaign import Campaign
        db = _Session()
        try:
            assert db.get(Campaign, cid) is None
        finally:
            db.close()


class TestPortfolioEdit:
    def test_category_update(self, client):
        email = _login(client)
        client.post("/ui/portfolio/categories", data={"name": "舊分類", "sort_order": "0"})
        from saas_mvp.models.portfolio_category import PortfolioCategory
        db = _Session()
        try:
            tid = _tenant_id_for(email)
            cat_id = db.query(PortfolioCategory).filter(
                PortfolioCategory.tenant_id == tid
            ).first().id
        finally:
            db.close()
        r = client.get(f"/ui/portfolio/categories/{cat_id}/edit")
        assert r.status_code == 200 and "舊分類" in r.text
        r2 = client.post(f"/ui/portfolio/categories/{cat_id}/update",
                         data={"name": "新分類", "sort_order": "2"})
        assert "新分類" in r2.text

    def test_item_update(self, client):
        email = _login(client)
        client.post("/ui/portfolio/items", data={
            "image_url": "https://x/a.jpg", "caption": "舊圖說",
            "category_id": "", "sort_order": "0",
        })
        from saas_mvp.models.portfolio_item import PortfolioItem
        db = _Session()
        try:
            tid = _tenant_id_for(email)
            item_id = db.query(PortfolioItem).filter(
                PortfolioItem.tenant_id == tid
            ).first().id
        finally:
            db.close()
        r = client.get(f"/ui/portfolio/items/{item_id}/edit")
        assert r.status_code == 200 and "舊圖說" in r.text
        r2 = client.post(f"/ui/portfolio/items/{item_id}/update", data={
            "image_url": "https://x/b.jpg", "caption": "新圖說", "sort_order": "1",
        })
        assert "新圖說" in r2.text


class TestFlexCardEdit:
    def test_card_update(self, client):
        email = _login(client)
        client.post("/ui/flex-menu/cards", data={
            "title": "舊卡", "action_type": "uri", "action_data": "https://e.com",
            "subtitle": "舊副標", "image_url": "", "bg_color": "",
        })
        from saas_mvp.models.flex_menu_card import FlexMenuCard
        db = _Session()
        try:
            tid = _tenant_id_for(email)
            card_id = db.query(FlexMenuCard).filter(
                FlexMenuCard.tenant_id == tid, FlexMenuCard.title == "舊卡"
            ).first().id
        finally:
            db.close()
        r = client.get(f"/ui/flex-menu/cards/{card_id}/edit")
        assert r.status_code == 200 and "舊卡" in r.text
        r2 = client.post(f"/ui/flex-menu/cards/{card_id}/update", data={
            "title": "新卡", "action_type": "uri", "action_data": "https://e2.com",
            "subtitle": "新副標", "image_url": "", "bg_color": "",
        })
        assert "新卡" in r2.text


class TestLocationStaffDelete:
    def _create_location(self, client, email, name="待刪分店"):
        client.post("/ui/locations", data={"name": name, "address": "", "phone": ""})
        from saas_mvp.models.location import Location
        db = _Session()
        try:
            tid = _tenant_id_for(email)
            return db.query(Location).filter(
                Location.tenant_id == tid, Location.name == name
            ).first().id
        finally:
            db.close()

    def test_location_delete_happy(self, client):
        email = _login(client)
        lid = self._create_location(client, email)
        assert client.post(f"/ui/locations/{lid}/delete").status_code == 200
        from saas_mvp.models.location import Location
        db = _Session()
        try:
            assert db.get(Location, lid) is None
        finally:
            db.close()

    def test_location_delete_blocked_with_staff(self, client):
        email = _login(client)
        lid = self._create_location(client, email, "有員工分店")
        client.post("/ui/staff", data={
            "name": "小華", "role": "設計師",
            "location_id": str(lid), "booking_mode": "capacity",
        })
        r = client.post(f"/ui/locations/{lid}/delete")
        assert r.status_code == 200 and "員工綁定" in r.text
        from saas_mvp.models.location import Location
        db = _Session()
        try:
            assert db.get(Location, lid) is not None
        finally:
            db.close()

    def test_staff_delete_happy(self, client):
        email = _login(client)
        client.post("/ui/staff", data={
            "name": "待刪員工", "role": "", "location_id": "", "booking_mode": "capacity",
        })
        from saas_mvp.models.staff import Staff
        db = _Session()
        try:
            tid = _tenant_id_for(email)
            sid = db.query(Staff).filter(
                Staff.tenant_id == tid, Staff.name == "待刪員工"
            ).first().id
        finally:
            db.close()
        assert client.post(f"/ui/staff/{sid}/delete").status_code == 200
        db = _Session()
        try:
            assert db.get(Staff, sid) is None
        finally:
            db.close()


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
