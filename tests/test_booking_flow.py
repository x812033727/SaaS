"""引導式預約對話流程測試 — parse + webhook 端到端（服務→員工→時段→確認）+ 降級。"""

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
import saas_mvp.models.staff as _staff  # noqa: F401,E402
import saas_mvp.models.service as _svc  # noqa: F401,E402
import saas_mvp.models.service_staff as _ss  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.booking.commands import parse_postback_data  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.line_client import FakeLineReplyClient, get_line_client  # noqa: E402
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.line_channel_config import LineChannelConfig  # noqa: E402
from saas_mvp.models.reservation import Reservation  # noqa: E402
from saas_mvp.models.service import Service  # noqa: E402
from saas_mvp.models.service_staff import ServiceStaff  # noqa: E402
from saas_mvp.models.staff import Staff  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.translation import get_translator  # noqa: E402
from saas_mvp.translation.stub import StubTranslator  # noqa: E402

_CHANNEL_SECRET = "flow_secret_value_0123456789abcdef"
_ACCESS_TOKEN = "flow_access_token_value"

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


# ─────────────────────────────── parse 測試 ──────────────────────────────────

class TestParse:
    def test_pick_service(self):
        action, params = parse_postback_data("action=pick_service&service_id=5")
        assert action == "pick_service" and params == {"service_id": 5}

    def test_pick_staff_with_and_without_staff(self):
        a, p = parse_postback_data("action=pick_staff&service_id=5&staff_id=9")
        assert a == "pick_staff" and p == {"service_id": 5, "staff_id": 9}
        a, p = parse_postback_data("action=pick_staff&service_id=5")
        assert a == "pick_staff" and p == {"service_id": 5}

    def test_pick_slot_carries_state(self):
        a, p = parse_postback_data(
            "action=pick_slot&service_id=5&staff_id=9&slot_id=12"
        )
        assert a == "pick_slot"
        assert p["service_id"] == 5 and p["staff_id"] == 9 and p["slot_id"] == 12


# ─────────────────────────────── webhook 端到端 ──────────────────────────────

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


def _seed(*, with_service=True, with_staff=True, with_slot=True) -> dict:
    db = _Session()
    try:
        t = Tenant(name=f"flow_{os.urandom(3).hex()}", plan="free")
        db.add(t)
        db.flush()
        cfg = LineChannelConfig(tenant_id=t.id, default_target_lang="zh-TW")
        cfg.channel_secret = _CHANNEL_SECRET
        cfg.access_token = _ACCESS_TOKEN
        cfg.bot_mode = "booking"
        db.add(cfg)
        out = {"tenant_id": t.id}
        if with_slot:
            slot = BookingSlot(
                tenant_id=t.id,
                slot_start=datetime.datetime(
                    2030, 6, 1, 18, 0, tzinfo=datetime.timezone.utc
                ),
                max_capacity=4,
            )
            db.add(slot)
            db.flush()
            out["slot_id"] = slot.id
        if with_service:
            svc = Service(tenant_id=t.id, name="剪髮", duration_minutes=30, price_cents=500)
            db.add(svc)
            db.flush()
            out["service_id"] = svc.id
            if with_staff:
                st = Staff(tenant_id=t.id, name="小美")
                db.add(st)
                db.flush()
                out["staff_id"] = st.id
                db.add(ServiceStaff(tenant_id=t.id, service_id=svc.id, staff_id=st.id))
        db.commit()
        return out
    finally:
        db.close()


def _text_event(text, *, user="Uflow", token="rt", eid="e") -> dict:
    return {
        "type": "message",
        "replyToken": token,
        "source": {"type": "user", "userId": user},
        "message": {"type": "text", "text": text},
        "webhookEventId": eid,
    }


def _postback_event(data, *, user="Uflow", token="rt", eid="e") -> dict:
    return {
        "type": "postback",
        "replyToken": token,
        "source": {"type": "user", "userId": user},
        "postback": {"data": data},
        "webhookEventId": eid,
    }


def _post(client, tenant_id, *events):
    body = json.dumps({"destination": "x", "events": list(events)}).encode()
    mac = hmac.new(_CHANNEL_SECRET.encode(), body, hashlib.sha256)
    sig = base64.b64encode(mac.digest()).decode()
    r = client.post(
        f"/line/webhook/{tenant_id}",
        content=body,
        headers={"X-Line-Signature": sig, "Content-Type": "application/json"},
    )
    assert r.status_code == 200, r.text


def _reservations(tenant_id):
    db = _Session()
    try:
        return list(
            db.execute(
                select(Reservation).where(Reservation.tenant_id == tenant_id)
            ).scalars()
        )
    finally:
        db.close()


class TestConversationalFlow:
    def test_book_shows_service_carousel(self, app_client):
        client, lc = app_client
        s = _seed()
        _post(client, s["tenant_id"], _text_event("預約", eid="f1"))
        # 應送出 Flex carousel（服務清單），非純文字
        assert lc.flex, "expected a flex carousel reply"
        carousel = lc.flex[-1].contents
        assert carousel["type"] == "carousel"
        data = carousel["contents"][0]["footer"]["contents"][0]["action"]["data"]
        assert f"pick_service&service_id={s['service_id']}" in data

    def test_pick_service_shows_staff_buttons(self, app_client):
        client, lc = app_client
        s = _seed()
        _post(
            client, s["tenant_id"],
            _postback_event(f"action=pick_service&service_id={s['service_id']}", eid="f2"),
        )
        last = lc.sent[-1]
        assert "服務人員" in last.text
        datas = [d for _l, d in (last.quick_reply or [])]
        # 含「不指定」與指定員工
        assert any("action=pick_staff" in d and "staff_id" not in d for d in datas)
        assert any(f"staff_id={s['staff_id']}" in d for d in datas)

    def test_pick_staff_shows_slots(self, app_client):
        client, lc = app_client
        s = _seed()
        _post(
            client, s["tenant_id"],
            _postback_event(
                f"action=pick_staff&service_id={s['service_id']}&staff_id={s['staff_id']}",
                eid="f3",
            ),
        )
        last = lc.sent[-1]
        assert "請選擇時段" in last.text
        datas = [d for _l, d in (last.quick_reply or [])]
        assert any(
            f"slot_id={s['slot_id']}" in d and f"service_id={s['service_id']}" in d
            for d in datas
        )

    def test_full_flow_creates_reservation_with_service_and_staff(self, app_client):
        client, lc = app_client
        s = _seed()
        tid = s["tenant_id"]
        _post(client, tid, _text_event("預約", eid="g1"))
        _post(
            client, tid,
            _postback_event(f"action=pick_service&service_id={s['service_id']}", eid="g2"),
        )
        _post(
            client, tid,
            _postback_event(
                f"action=pick_staff&service_id={s['service_id']}&staff_id={s['staff_id']}",
                eid="g3",
            ),
        )
        _post(
            client, tid,
            _postback_event(
                f"action=pick_slot&service_id={s['service_id']}"
                f"&staff_id={s['staff_id']}&slot_id={s['slot_id']}",
                eid="g4",
            ),
        )
        rows = _reservations(tid)
        assert len(rows) == 1
        assert rows[0].service_id == s["service_id"]
        assert rows[0].staff_id == s["staff_id"]
        assert "預約成功" in (lc.last_text or "")
        assert "Google 行事曆" in (lc.last_text or "")

    def test_any_staff_books_without_staff_id(self, app_client):
        client, lc = app_client
        s = _seed()
        tid = s["tenant_id"]
        _post(
            client, tid,
            _postback_event(
                f"action=pick_slot&service_id={s['service_id']}&slot_id={s['slot_id']}",
                eid="a1",
            ),
        )
        rows = _reservations(tid)
        assert len(rows) == 1
        assert rows[0].service_id == s["service_id"]
        assert rows[0].staff_id is None


class TestGracefulDegradation:
    def test_no_services_falls_back_to_raw_slot_flow(self, app_client):
        """無服務時，'預約' 退回既有時段選擇流程（quick-reply），仍可建單。"""
        client, lc = app_client
        s = _seed(with_service=False, with_staff=False)
        tid = s["tenant_id"]
        # 「預約」無參數 → 既有 _prompt_choose_slot（slot quick-reply），非 flex
        _post(client, tid, _text_event("預約", eid="d1"))
        assert not lc.flex
        assert "請選擇時段" in (lc.last_text or "")
        # raw-slot 一次性預約仍可建單
        _post(client, tid, _text_event(f"預約 {s['slot_id']} 2", eid="d2"))
        rows = _reservations(tid)
        assert len(rows) == 1 and rows[0].party_size == 2
