"""新功能管理 UI（/ui/locations, /staff, /services, /campaigns, /flex-menu,
/portfolio, /profile, /pos, /faq）測試。

鏡像 tests/test_ui*.py：in-memory engine + register/login 取 cookie，再驅動頁面。
預設租戶所有進階功能皆開通（features_default_enabled=True），feature-locked 情境
以 /ui/features/{F}/unsubscribe 明確關閉。
"""

from __future__ import annotations

import datetime
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
    global _last_login_email
    _last_login_email = email
    return email


def _tenant_id_for(email: str) -> int:
    db = _Session()
    try:
        return db.query(User).filter(User.email == email).first().tenant_id
    finally:
        db.close()


_last_login_email: str | None = None


def _disable(client, feature: str) -> None:
    """關閉最近登入租戶的進階功能。

    R12-C3a:/ui/features 頁已實體刪除,改走 service 層(feature 閘門本身
    由各端點的 require_feature / _require_ui_feature 測試覆蓋)。
    """
    del client  # 介面相容:呼叫端習慣傳 client
    from saas_mvp.services import features as features_svc

    db = _Session()
    try:
        tid = _tenant_id_for(_last_login_email)
        features_svc.set_enabled(
            db, tid, feature, False, actor_user_id=None, source="test"
        )
        db.commit()
    finally:
        db.close()


def _utc_today_iso() -> str:
    """app 端(commissions/POS)以 UTC 日期記帳;測試日期必須對齊,
    否則本地時區超前 UTC 的清晨時段(台北 00:00-08:00)date.today() 會多一天,
    抽成規則 effective_from 落在未來 → 抽成不生效 → flake。"""
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


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


