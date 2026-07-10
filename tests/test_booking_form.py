"""網頁預約表單（A1.1）+ datetimepicker（A1.3）測試。

覆蓋:
- token 簽發/解析/過期/已用
- 漸進式表單三步(服務→日期→時段)與無服務降級
- POST 建單:走既有 book_slot、token 標 used、重放回 used 頁
- 額滿:錯誤頁 + token 未消耗(可重試)
- webhook:WEB_BOOKING 開通時 quick-reply 附「用網頁預約」URI dict
- datetimepicker:日期按鈕含 picker dict;postback params.date 驅動 pick_date
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.models import tenant as _t, user as _u  # noqa: F401,E402
from saas_mvp.models import customer as _c, booking_slot as _bs  # noqa: F401,E402
from saas_mvp.models import reservation as _r  # noqa: F401,E402
from saas_mvp.models import booking_form_token as _bft  # noqa: F401,E402
import saas_mvp.models.line_channel_config as _lcm  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.config import settings  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.line_client import FakeLineReplyClient, get_line_client  # noqa: E402
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.line_channel_config import LineChannelConfig  # noqa: E402
from saas_mvp.models.reservation import Reservation  # noqa: E402
from saas_mvp.models.service import Service  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.services import booking_form as bf_svc  # noqa: E402
from saas_mvp.services import features as features_svc  # noqa: E402
from saas_mvp.translation import get_translator  # noqa: E402
from saas_mvp.translation.stub import StubTranslator  # noqa: E402

_CHANNEL_SECRET = "webform_secret_value_0123456789abcd"

_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_SLOT_START = datetime.datetime(2030, 6, 1, 18, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    line_client = FakeLineReplyClient()
    app = create_app()

    def override_db():
        s = _Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_line_client] = lambda: line_client
    app.dependency_overrides[get_translator] = lambda: StubTranslator()
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c, line_client


def _seed(*, with_service=True, capacity=4) -> tuple[int, int | None, int]:
    """回傳 (tenant_id, service_id, slot_id)。"""
    db = _Session()
    try:
        t = Tenant(name=f"wf_{uuid.uuid4().hex[:8]}", plan="free")
        db.add(t)
        db.flush()
        cfg = LineChannelConfig(tenant_id=t.id, default_target_lang="zh-TW")
        cfg.channel_secret = _CHANNEL_SECRET
        cfg.access_token = "tok"
        cfg.bot_mode = "booking"
        db.add(cfg)
        service_id = None
        if with_service:
            svc = Service(tenant_id=t.id, name="剪髮", duration_minutes=60, price_cents=80000)
            db.add(svc)
            db.flush()
            service_id = svc.id
        slot = BookingSlot(
            tenant_id=t.id, slot_start=_SLOT_START,
            slot_end=_SLOT_START + datetime.timedelta(hours=2),
            max_capacity=capacity,
        )
        db.add(slot)
        db.flush()
        slot_id = slot.id
        db.commit()
        return t.id, service_id, slot_id
    finally:
        db.close()


def _issue(tenant_id: int, user="Uwebform") -> str:
    db = _Session()
    try:
        row = bf_svc.issue_token(db, tenant_id=tenant_id, line_user_id=user)
        return row.token
    finally:
        db.close()


# ── 表單流程 ─────────────────────────────────────────────────────────────────

class TestFormFlow:
    def test_three_steps_and_booking(self, client):
        c, _ = client
        tid, sid, slot_id = _seed()
        token = _issue(tid)

        r = c.get(f"/booking/f/{token}")
        assert r.status_code == 200 and "選擇服務" in r.text and "剪髮" in r.text

        r = c.get(f"/booking/f/{token}", params={"service_id": sid})
        assert "選擇日期" in r.text and "2030-06-01" in r.text

        r = c.get(f"/booking/f/{token}", params={"service_id": sid, "date": "2030-06-01"})
        assert "選擇時段" in r.text and "18:00" in r.text

        r = c.post(f"/booking/f/{token}", data={
            "slot_id": slot_id, "party_size": 2, "service_id": sid,
        })
        assert "預約完成" in r.text
        db = _Session()
        try:
            resv = db.execute(
                select(Reservation).where(Reservation.tenant_id == tid)
            ).scalar_one()
            assert resv.party_size == 2
            assert resv.line_user_id == "Uwebform"
            assert resv.service_id == sid
        finally:
            db.close()

        # token 一次性:再開回 used 頁
        r = c.get(f"/booking/f/{token}")
        assert "已完成預約" in r.text

    def test_no_service_goes_straight_to_dates(self, client):
        c, _ = client
        tid, _, _ = _seed(with_service=False)
        token = _issue(tid)
        r = c.get(f"/booking/f/{token}")
        assert "選擇日期" in r.text

    def test_unknown_token_404(self, client):
        c, _ = client
        r = c.get("/booking/f/nope")
        assert r.status_code == 404

    def test_expired_token_page(self, client):
        c, _ = client
        tid, _, _ = _seed()
        token = _issue(tid)
        db = _Session()
        try:
            from saas_mvp.models.booking_form_token import BookingFormToken

            row = db.execute(
                select(BookingFormToken).where(BookingFormToken.token == token)
            ).scalar_one()
            row.expires_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)
            db.commit()
        finally:
            db.close()
        r = c.get(f"/booking/f/{token}")
        assert "已過期" in r.text

    def test_slot_full_error_keeps_token_retryable(self, client):
        c, _ = client
        tid, sid, slot_id = _seed(capacity=1)
        # 先塞滿
        token1 = _issue(tid, user="Ufirst")
        c.post(f"/booking/f/{token1}", data={"slot_id": slot_id, "party_size": 1})
        # 第二人撞額滿
        token2 = _issue(tid, user="Usecond")
        r = c.post(f"/booking/f/{token2}", data={"slot_id": slot_id, "party_size": 1})
        assert "額滿" in r.text
        # token 未消耗,可回上一步重選
        r = c.get(f"/booking/f/{token2}")
        assert r.status_code == 200 and "已完成預約" not in r.text


# ── webhook 整合 ─────────────────────────────────────────────────────────────

def _post_webhook(c, tenant_id: int, event: dict) -> None:
    body = json.dumps({"destination": "x", "events": [event]}).encode()
    mac = hmac.new(_CHANNEL_SECRET.encode(), body, hashlib.sha256)
    sig = base64.b64encode(mac.digest()).decode()
    r = c.post(
        f"/line/webhook/{tenant_id}",
        content=body,
        headers={"X-Line-Signature": sig, "Content-Type": "application/json"},
    )
    assert r.status_code == 200, r.text


def _text_event(text: str, *, user="Uwebhookf", eid="e1") -> dict:
    return {
        "type": "message", "replyToken": "rt", "webhookEventId": eid,
        "source": {"type": "user", "userId": user},
        "message": {"type": "text", "text": text},
    }


class TestWebhookIntegration:
    def test_quick_reply_gets_web_booking_uri(self, client, monkeypatch):
        monkeypatch.setattr(settings, "public_base_url", "https://shop.example")
        monkeypatch.setattr(settings, "features_default_enabled", False)
        c, line = client
        tid, _, _ = _seed()
        db = _Session()
        try:
            features_svc.set_enabled(
                db, tid, features_svc.WEB_BOOKING, True,
                actor_user_id=None, source="admin",
            )
        finally:
            db.close()
        _post_webhook(c, tid, _text_event("我的預約"))
        # 「我的預約」無預約 → 文字回覆;應附網頁預約 URI 按鈕
        qr = line.sent[-1].quick_reply or []
        uri_items = [i for i in qr if isinstance(i, dict) and i.get("type") == "uri"]
        assert uri_items and "/booking/f/" in uri_items[0]["uri"]

    def test_no_uri_when_feature_disabled(self, client, monkeypatch):
        monkeypatch.setattr(settings, "public_base_url", "https://shop.example")
        monkeypatch.setattr(settings, "features_default_enabled", False)
        c, line = client
        tid, _, _ = _seed()
        _post_webhook(c, tid, _text_event("我的預約"))
        qr = line.sent[-1].quick_reply or []
        assert not any(isinstance(i, dict) and i.get("type") == "uri" for i in qr)

    def test_date_buttons_include_datetimepicker(self, client, monkeypatch):
        monkeypatch.setattr(settings, "features_default_enabled", True)
        c, line = client
        tid, sid, _ = _seed()
        _post_webhook(c, tid, {
            "type": "postback", "replyToken": "rt", "webhookEventId": "e2",
            "source": {"type": "user", "userId": "Upicker"},
            "postback": {"data": f"action=pick_service&service_id={sid}"},
        })
        qr = line.sent[-1].quick_reply or []
        pickers = [i for i in qr if isinstance(i, dict) and i.get("type") == "datetimepicker"]
        assert pickers and pickers[0]["mode"] == "date"

    def test_datetimepicker_params_date_drives_pick_date(self, client, monkeypatch):
        monkeypatch.setattr(settings, "features_default_enabled", True)
        c, line = client
        tid, sid, _ = _seed()
        _post_webhook(c, tid, {
            "type": "postback", "replyToken": "rt", "webhookEventId": "e3",
            "source": {"type": "user", "userId": "Upicker2"},
            # datetimepicker 的 data 不帶 date,LINE 放在 params.date
            "postback": {
                "data": f"action=pick_date&service_id={sid}",
                "params": {"date": "2030-06-01"},
            },
        })
        # pick_date → 下一步是選服務人員
        assert "服務人員" in (line.sent[-1].text or "")


# ── http client 轉換 ─────────────────────────────────────────────────────────

def test_quick_reply_items_mixed_tuple_and_dict():
    from saas_mvp.line_client.http import _quick_reply_items

    items = _quick_reply_items([
        ("預約", "action=book"),
        {"type": "uri", "label": "網頁", "uri": "https://x.example/f/t"},
        {"type": "datetimepicker", "label": "挑日期", "data": "action=pick_date",
         "mode": "date"},
    ])
    assert items[0]["action"]["type"] == "postback"
    assert items[1]["action"]["type"] == "uri"
    assert items[2]["action"]["type"] == "datetimepicker"
