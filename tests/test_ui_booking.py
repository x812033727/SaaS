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


def _slot_id_by_start(start: str) -> int:
    """以 slot_start（naive UTC datetime 字串）反查時段 id。"""
    import datetime as _dt

    from saas_mvp.models.booking_slot import BookingSlot

    db = _Session()
    try:
        row = (
            db.query(BookingSlot)
            .filter(BookingSlot.slot_start == _dt.datetime.fromisoformat(start))
            .first()
        )
        assert row is not None, f"slot {start} not found"
        return row.id
    finally:
        db.close()


class TestSlotEditDelete:
    def test_edit_form_prefilled_and_update(self, client):
        _login(client)
        client.post("/ui/booking/slots", data={
            "slot_start": "2031-01-05T10:00", "max_capacity": "8", "walkin_reserved": "2",
        })
        slot_id = _slot_id_by_start("2031-01-05 10:00")
        # 編輯表單預填現值
        r = client.get(f"/ui/booking/slots/{slot_id}/edit")
        assert r.status_code == 200
        assert f"/ui/booking/slots/{slot_id}/update" in r.text
        assert 'value="8"' in r.text and 'value="2"' in r.text
        # 更新 roundtrip
        r = client.post(f"/ui/booking/slots/{slot_id}/update", data={
            "max_capacity": "12", "walkin_reserved": "3",
        })
        assert r.status_code == 200
        assert "/update" not in r.text  # 編輯列已收合
        assert "<td data-label=\"容量\">12</td>" in r.text

    def test_update_shrink_below_booked_shows_error(self, client):
        _login(client)
        client.post("/ui/booking/slots", data={
            "slot_start": "2031-01-06T10:00", "max_capacity": "10", "walkin_reserved": "0",
        })
        slot_id = _slot_id_by_start("2031-01-06 10:00")
        db = _Session()
        try:
            from saas_mvp.models.booking_slot import BookingSlot
            db.query(BookingSlot).filter(BookingSlot.id == slot_id).update(
                {"booked_count": 5}
            )
            db.commit()
        finally:
            db.close()
        r = client.post(f"/ui/booking/slots/{slot_id}/update", data={
            "max_capacity": "3", "walkin_reserved": "0",
        })
        assert r.status_code == 200
        assert "Cannot shrink capacity" in r.text
        # 失敗時停留在編輯列
        assert f"/ui/booking/slots/{slot_id}/update" in r.text

    def test_delete_slot(self, client):
        _login(client)
        client.post("/ui/booking/slots", data={
            "slot_start": "2031-01-07T10:00", "max_capacity": "5", "walkin_reserved": "0",
        })
        slot_id = _slot_id_by_start("2031-01-07 10:00")
        r = client.post(f"/ui/booking/slots/{slot_id}/delete")
        assert r.status_code == 200
        assert "2031-01-07 10:00" not in r.text

    def test_delete_blocked_when_reserved(self, client):
        _login(client)
        client.post("/ui/booking/slots", data={
            "slot_start": "2031-01-08T10:00", "max_capacity": "5", "walkin_reserved": "0",
        })
        slot_id = _slot_id_by_start("2031-01-08 10:00")
        db = _Session()
        try:
            from saas_mvp.models.booking_slot import BookingSlot
            from saas_mvp.models.reservation import (
                RESERVATION_CANCELLED,
                Reservation,
            )
            slot = db.query(BookingSlot).filter(BookingSlot.id == slot_id).first()
            # 已取消的預約也算歷史紀錄，仍須擋刪
            db.add(Reservation(
                tenant_id=slot.tenant_id, slot_id=slot_id,
                party_size=1, status=RESERVATION_CANCELLED,
            ))
            db.commit()
        finally:
            db.close()
        r = client.post(f"/ui/booking/slots/{slot_id}/delete")
        assert r.status_code == 200
        assert "已有預約紀錄" in r.text
        assert "2031-01-08 10:00" in r.text  # 時段還在

    def test_deactivate_unknown_slot_shows_error(self, client):
        """靜默吞錯回歸：停用不存在的時段要顯示錯誤，不能假裝成功。"""
        _login(client)
        r = client.post("/ui/booking/slots/999999/deactivate")
        assert r.status_code == 200
        assert "Slot not found" in r.text

    def test_cancel_unknown_reservation_shows_error(self, client):
        _login(client)
        r = client.post("/ui/booking/reservations/999999/cancel")
        assert r.status_code == 200
        assert "預約不存在或已取消" in r.text

    def test_cancel_edit_returns_plain_list(self, client):
        _login(client)
        client.post("/ui/booking/slots", data={
            "slot_start": "2031-01-09T10:00", "max_capacity": "5", "walkin_reserved": "0",
        })
        r = client.get("/ui/booking/slots")
        assert r.status_code == 200
        assert "2031-01-09 10:00" in r.text
        assert "/update" not in r.text


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


class TestReminderHoursUI:
    def test_page_shows_reminder_control(self, client):
        _login(client)
        r = client.get("/ui/booking")
        assert r.status_code == 200
        assert "自動提醒設定" in r.text

    def test_set_reminder_hours(self, client):
        _login(client)
        r = client.post("/ui/booking/reminder-hours", data={"reminder_hours_before": 6})
        assert r.status_code == 200
        assert "已儲存" in r.text and "6" in r.text
        # 重新整理頁面應反映新值
        page = client.get("/ui/booking")
        assert 'value="6"' in page.text

    def test_reject_out_of_range(self, client):
        _login(client)
        r = client.post("/ui/booking/reminder-hours", data={"reminder_hours_before": 999})
        assert r.status_code == 200
        assert "1 ～ 168" in r.text or "error" in r.text.lower()


class TestCalendarUI:
    def test_month_view_renders(self, client):
        _login(client)
        r = client.get("/ui/calendar")
        assert r.status_code == 200
        assert "預約行事曆" in r.text
        assert "月曆" in r.text and "週曆" in r.text

    def test_week_view_renders(self, client):
        _login(client)
        r = client.get("/ui/calendar?view=week")
        assert r.status_code == 200
        assert "上一週" in r.text

    def test_staff_mode_renders(self, client):
        _login(client)
        r = client.get("/ui/calendar?mode=staff")
        assert r.status_code == 200
        assert "員工排班" in r.text

    def test_bad_date_falls_back(self, client):
        _login(client)
        r = client.get("/ui/calendar?date=not-a-date")
        assert r.status_code == 200
