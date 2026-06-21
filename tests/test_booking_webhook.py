"""LINE webhook 預約模式測試 — bot_mode 分流、指令/postback 建單、翻譯回歸。

背景任務在 TestClient 內於 response 前跑完（見 line_webhook 模組 docstring），
故 POST 回來後即可斷言 reply 與 DB 副作用。
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.models import tenant as _t, user as _u  # noqa: F401,E402
from saas_mvp.models import customer as _c, booking_slot as _bs  # noqa: F401,E402
from saas_mvp.models import reservation as _r, reservation_reminder as _rr  # noqa: F401,E402
import saas_mvp.models.line_channel_config as _lcm  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.line_client import FakeLineReplyClient, get_line_client  # noqa: E402
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.customer import Customer  # noqa: E402
from saas_mvp.models.line_channel_config import LineChannelConfig  # noqa: E402
from saas_mvp.models.reservation import Reservation  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.translation import get_translator  # noqa: E402
from saas_mvp.translation.stub import StubTranslator  # noqa: E402

_CHANNEL_SECRET = "booking_secret_value_0123456789abcd"
_ACCESS_TOKEN = "booking_access_token_value"

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture()
def app_client():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    line_client = FakeLineReplyClient()
    app = create_app()

    def override_db():
        db = _Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_line_client] = lambda: line_client
    app.dependency_overrides[get_translator] = lambda: StubTranslator()

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c, line_client


def _seed(bot_mode: str, *, with_slot: bool = False) -> tuple[int, int | None]:
    db = _Session()
    try:
        t = Tenant(name=f"bk_{bot_mode}_{os.urandom(3).hex()}", plan="free")
        db.add(t)
        db.flush()
        cfg = LineChannelConfig(tenant_id=t.id, default_target_lang="zh-TW")
        cfg.channel_secret = _CHANNEL_SECRET
        cfg.access_token = _ACCESS_TOKEN
        cfg.bot_mode = bot_mode
        db.add(cfg)
        slot_id = None
        if with_slot:
            slot = BookingSlot(
                tenant_id=t.id,
                slot_start=datetime.datetime(2030, 6, 1, 18, 0, tzinfo=datetime.timezone.utc),
                max_capacity=4,
            )
            db.add(slot)
            db.flush()
            slot_id = slot.id
        db.commit()
        return t.id, slot_id
    finally:
        db.close()


def _text_event(text: str, *, user="Uwebhook", token="rtok", eid="evt1") -> dict:
    return {
        "type": "message",
        "replyToken": token,
        "source": {"type": "user", "userId": user},
        "message": {"type": "text", "text": text},
        "webhookEventId": eid,
    }


def _postback_event(data: str, *, user="Uwebhook", token="rtok", eid="evt2") -> dict:
    return {
        "type": "postback",
        "replyToken": token,
        "source": {"type": "user", "userId": user},
        "postback": {"data": data},
        "webhookEventId": eid,
    }


def _post(client, tenant_id: int, *events) -> None:
    body = json.dumps({"destination": "x", "events": list(events)}).encode()
    mac = hmac.new(_CHANNEL_SECRET.encode(), body, hashlib.sha256)
    sig = base64.b64encode(mac.digest()).decode()
    r = client.post(
        f"/line/webhook/{tenant_id}",
        content=body,
        headers={"X-Line-Signature": sig, "Content-Type": "application/json"},
    )
    assert r.status_code == 200, r.text


def _reservations(tenant_id: int) -> list[Reservation]:
    db = _Session()
    try:
        return list(
            db.execute(
                select(Reservation).where(Reservation.tenant_id == tenant_id)
            ).scalars()
        )
    finally:
        db.close()


class TestBookingMode:
    def test_text_book_creates_reservation(self, app_client):
        client, line_client = app_client
        tid, sid = _seed("booking", with_slot=True)
        _post(client, tid, _text_event(f"預約 {sid} 2"))
        rows = _reservations(tid)
        assert len(rows) == 1
        assert rows[0].party_size == 2
        assert "預約成功" in (line_client.last_text or "")
        # 顧客自動建檔
        db = _Session()
        try:
            customers = db.execute(
                select(Customer).where(Customer.tenant_id == tid)
            ).scalars().all()
            assert len(customers) == 1 and customers[0].booking_count == 1
        finally:
            db.close()

    def test_postback_book_creates_reservation(self, app_client):
        client, line_client = app_client
        tid, sid = _seed("booking", with_slot=True)
        _post(client, tid, _postback_event(f"action=book&slot_id={sid}&party=1"))
        assert len(_reservations(tid)) == 1

    def test_slots_query_offers_quick_reply_buttons(self, app_client):
        """「時段」→ 引導式：回「請選擇時段」+ pick_slot quick-reply 按鈕。"""
        client, line_client = app_client
        tid, sid = _seed("booking", with_slot=True)
        _post(client, tid, _text_event("時段"))
        last = line_client.sent[-1]
        assert "請選擇時段" in last.text
        assert last.quick_reply is not None
        datas = [data for _label, data in last.quick_reply]
        assert any(f"slot_id={sid}" in d and "pick_slot" in d for d in datas)

    def test_guided_pick_slot_offers_party_buttons(self, app_client):
        """postback pick_slot → 回「請選擇人數」+ book quick-reply 按鈕。"""
        client, line_client = app_client
        tid, sid = _seed("booking", with_slot=True)
        _post(client, tid, _postback_event(f"action=pick_slot&slot_id={sid}", eid="g1"))
        last = line_client.sent[-1]
        assert "請選擇人數" in last.text
        assert last.quick_reply is not None
        datas = [data for _label, data in last.quick_reply]
        # party 按鈕帶 slot_id + party，且 action=book
        assert any(f"action=book&slot_id={sid}&party=" in d for d in datas)
        # 尚未建單
        assert _reservations(tid) == []

    def test_guided_full_flow_books(self, app_client):
        """預約(無參數) → pick_slot → 選人數 → 建單。"""
        client, line_client = app_client
        tid, sid = _seed("booking", with_slot=True)
        _post(client, tid, _text_event("預約", eid="g1"))  # 選時段
        _post(client, tid, _postback_event(f"action=pick_slot&slot_id={sid}", eid="g2"))  # 選人數
        _post(client, tid, _postback_event(f"action=book&slot_id={sid}&party=2", eid="g3"))  # 建單
        rows = _reservations(tid)
        assert len(rows) == 1 and rows[0].party_size == 2
        assert "預約成功" in (line_client.last_text or "")

    def test_full_slot_friendly_reject(self, app_client):
        client, line_client = app_client
        tid, sid = _seed("booking", with_slot=True)
        # 先把 4 容量填滿
        _post(client, tid, _text_event(f"預約 {sid} 4", eid="e1", user="U1"))
        _post(client, tid, _text_event(f"預約 {sid} 1", eid="e2", user="U2"))
        assert "額滿" in (line_client.last_text or "")
        assert len(_reservations(tid)) == 1  # 第二筆未建立

    def test_cancel_via_text(self, app_client):
        client, line_client = app_client
        tid, sid = _seed("booking", with_slot=True)
        _post(client, tid, _text_event(f"預約 {sid} 1", eid="e1"))
        rid = _reservations(tid)[0].id
        _post(client, tid, _text_event(f"取消 {rid}", eid="e2"))
        assert "已取消" in (line_client.last_text or "")
        db = _Session()
        try:
            assert db.get(Reservation, rid).status == "cancelled"
        finally:
            db.close()


class TestCouponsAndPoints:
    def _add_coupon(self, tid, code="SAVE10", max_redemptions=None):
        from saas_mvp.models.coupon import Coupon
        db = _Session()
        try:
            c = Coupon(
                tenant_id=tid, code=code, name="折扣", discount_type="percent",
                discount_value=10, max_redemptions=max_redemptions, is_active=True,
            )
            db.add(c)
            db.commit()
        finally:
            db.close()

    def test_coupons_list_offers_redeem_buttons(self, app_client):
        client, line_client = app_client
        tid, _ = _seed("booking")
        self._add_coupon(tid, code="SUMMER")
        _post(client, tid, _text_event("優惠券"))
        last = line_client.sent[-1]
        assert "SUMMER" in last.text
        assert last.quick_reply is not None
        assert any("action=redeem&code=SUMMER" in d for _l, d in last.quick_reply)

    def test_redeem_success_then_already(self, app_client):
        client, line_client = app_client
        tid, _ = _seed("booking")
        self._add_coupon(tid, code="ONCE")
        _post(client, tid, _text_event("兌換 ONCE", eid="r1"))
        assert "兌換成功" in (line_client.last_text or "")
        _post(client, tid, _text_event("兌換 ONCE", eid="r2"))
        assert "已兌換過" in (line_client.last_text or "")

    def test_redeem_unknown_code(self, app_client):
        client, line_client = app_client
        tid, _ = _seed("booking")
        _post(client, tid, _text_event("兌換 NOPE"))
        assert "找不到券碼" in (line_client.last_text or "")

    def test_points_after_booking(self, app_client):
        client, line_client = app_client
        tid, sid = _seed("booking", with_slot=True)
        _post(client, tid, _text_event(f"預約 {sid} 1", eid="b1"))
        _post(client, tid, _text_event("點數", eid="p1"))
        assert "點數" in (line_client.last_text or "")
        assert "10" in (line_client.last_text or "")


class TestShop:
    def _add_product(self, tid, *, name="珍奶", price=100, stock=None) -> int:
        from saas_mvp.models.product import Product
        db = _Session()
        try:
            p = Product(tenant_id=tid, name=name, price_cents=price, stock=stock, currency="TWD")
            db.add(p)
            db.commit()
            return p.id
        finally:
            db.close()

    def test_shop_lists_products_with_buy_buttons(self, app_client):
        client, line_client = app_client
        tid, _ = _seed("booking")
        pid = self._add_product(tid, name="拿鐵")
        _post(client, tid, _text_event("商品"))
        last = line_client.sent[-1]
        assert "拿鐵" in last.text
        assert last.quick_reply is not None
        assert any(f"action=buy&product_id={pid}&qty=1" in d for _l, d in last.quick_reply)

    def test_buy_creates_order_with_checkout(self, app_client):
        client, line_client = app_client
        tid, _ = _seed("booking")
        pid = self._add_product(tid, price=150, stock=10)
        _post(client, tid, _postback_event(f"action=buy&product_id={pid}&qty=2", eid="o1"))
        assert "已建立訂單" in (line_client.last_text or "")
        assert "付款連結" in (line_client.last_text or "")
        # 庫存扣減
        from saas_mvp.models.product import Product
        db = _Session()
        try:
            assert db.get(Product, pid).stock == 8
        finally:
            db.close()

    def test_buy_out_of_stock(self, app_client):
        client, line_client = app_client
        tid, _ = _seed("booking")
        pid = self._add_product(tid, stock=1)
        _post(client, tid, _postback_event(f"action=buy&product_id={pid}&qty=5", eid="o1"))
        assert "庫存不足" in (line_client.last_text or "")

    def test_my_orders(self, app_client):
        client, line_client = app_client
        tid, _ = _seed("booking")
        pid = self._add_product(tid, stock=10)
        _post(client, tid, _postback_event(f"action=buy&product_id={pid}&qty=1", eid="o1"))
        _post(client, tid, _text_event("我的訂單", eid="o2"))
        assert "你的訂單" in (line_client.last_text or "")


class TestFeatureGatingWebhook:
    def _disable(self, tid, feature):
        from saas_mvp.models.tenant_feature import TenantFeature
        db = _Session()
        try:
            db.add(TenantFeature(tenant_id=tid, feature=feature, enabled=False))
            db.commit()
        finally:
            db.close()

    def test_coupons_disabled_message(self, app_client):
        client, line_client = app_client
        tid, _ = _seed("booking")
        self._disable(tid, "COUPON_SYSTEM")
        _post(client, tid, _text_event("優惠券"))
        assert "尚未開放優惠券" in (line_client.last_text or "")

    def test_shop_disabled_message(self, app_client):
        client, line_client = app_client
        tid, _ = _seed("booking")
        self._disable(tid, "PRODUCT_SALES")
        _post(client, tid, _text_event("商品"))
        assert "尚未開放商品" in (line_client.last_text or "")


class TestTranslationRegression:
    def test_translation_mode_still_translates(self, app_client):
        """bot_mode 預設 translation → 仍走翻譯，不建立預約（回歸保護）。"""
        client, line_client = app_client
        tid, _ = _seed("translation")
        _post(client, tid, _text_event("hello"))
        # StubTranslator 輸出 [ZH-TW] 前綴
        assert line_client.last_text is not None
        assert line_client.last_text.startswith("[")
        assert _reservations(tid) == []
