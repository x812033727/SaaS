"""A2 AI 對話測試 — StubAgent 抽槽、多輪補槽、額度降級、webhook 端到端。"""

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
from saas_mvp.models import line_conversation as _lc, ai_usage as _au  # noqa: F401,E402
import saas_mvp.models.line_channel_config as _lcm  # noqa: F401,E402
import saas_mvp.models.service as _svc  # noqa: F401,E402

from saas_mvp.ai.agent import AgentTurn, StubAgent  # noqa: E402
from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.config import settings  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.line_client import FakeLineReplyClient, get_line_client  # noqa: E402
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.line_channel_config import LineChannelConfig  # noqa: E402
from saas_mvp.models.line_conversation import LineConversation  # noqa: E402
from saas_mvp.models.reservation import Reservation  # noqa: E402
from saas_mvp.models.service import Service  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.services import ai_conversation as conv_svc  # noqa: E402
from saas_mvp.services import ai_quota as ai_quota_svc  # noqa: E402
from saas_mvp.services import features as features_svc  # noqa: E402
from saas_mvp.translation import get_translator  # noqa: E402
from saas_mvp.translation.stub import StubTranslator  # noqa: E402

_CHANNEL_SECRET = "ai_secret_value_0123456789abcdefghi"

_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_SLOT_START = datetime.datetime(2030, 6, 1, 18, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    s = _Session()
    try:
        yield s
    finally:
        s.close()


def _seed(db) -> dict:
    t = Tenant(name=f"ai_{uuid.uuid4().hex[:8]}", plan="pro")
    db.add(t)
    db.flush()
    cfg = LineChannelConfig(tenant_id=t.id, default_target_lang="zh-TW")
    cfg.channel_secret = _CHANNEL_SECRET
    cfg.access_token = "tok"
    cfg.bot_mode = "booking"
    db.add(cfg)
    svc = Service(tenant_id=t.id, name="剪髮", duration_minutes=60, price_cents=80000)
    db.add(svc)
    db.flush()
    slot = BookingSlot(
        tenant_id=t.id, slot_start=_SLOT_START,
        slot_end=_SLOT_START + datetime.timedelta(hours=2), max_capacity=4,
    )
    db.add(slot)
    db.flush()
    out = {"tenant_id": t.id, "service_id": svc.id, "slot_id": slot.id}
    db.commit()
    return out


# ── StubAgent 抽取 ────────────────────────────────────────────────────────────

class TestStubAgent:
    def test_extracts_service_date_party(self):
        ctx = "服務項目：\n・id=3 剪髮 60分鐘 NT$800"
        turn = StubAgent().converse("我要約 2030-06-01 剪髮 2位", {}, ctx)
        assert turn.service_id == 3
        assert turn.date == "2030-06-01"
        assert turn.party_size == 2

    def test_no_match_empty(self):
        turn = StubAgent().converse("你們有停車場嗎", {}, "・id=3 剪髮")
        assert turn.service_id is None and turn.date is None


# ── 對話驅動 ─────────────────────────────────────────────────────────────────

class TestHandleFreeText:
    def test_flag_off_returns_none(self, db, monkeypatch):
        monkeypatch.setattr(settings, "features_default_enabled", False)
        s = _seed(db)
        db.get(Tenant, s["tenant_id"]).plan = "standard"  # standard 無 AI agent
        db.commit()
        assert conv_svc.handle_free_text(db, s["tenant_id"], "U1", "約剪髮") is None

    def test_full_slots_offers_pick_slot_buttons(self, db):
        s = _seed(db)
        out = conv_svc.handle_free_text(
            db, s["tenant_id"], "Uai1", "我要約 2030-06-01 剪髮 2位"
        )
        assert out is not None
        reply, buttons = out
        assert "18:00" in [b[0] for b in buttons]
        data = buttons[0][1]
        assert "action=pick_slot" in data
        assert f"service_id={s['service_id']}" in data
        assert "party=2" in data

    def test_multi_turn_slot_accumulation(self, db):
        s = _seed(db)
        # 第一輪只講服務 → 追問日期
        reply, buttons = conv_svc.handle_free_text(
            db, s["tenant_id"], "Uai2", "我想剪髮"
        )
        assert any("action=pick_date" in b[1] for b in (buttons or []))
        # 第二輪補日期 → 直接列時段（服務從對話狀態帶出）
        reply, buttons = conv_svc.handle_free_text(
            db, s["tenant_id"], "Uai2", "2030-06-01"
        )
        assert any("action=pick_slot" in b[1] for b in (buttons or []))
        conv = db.execute(
            select(LineConversation).where(
                LineConversation.line_user_id == "Uai2"
            )
        ).scalar_one()
        assert conv.turn_count == 2
        assert conv.slots["service_id"] == s["service_id"]

    def test_unavailable_date_suggests_alternatives(self, db):
        s = _seed(db)
        reply, buttons = conv_svc.handle_free_text(
            db, s["tenant_id"], "Uai3", "剪髮 2030-12-25"
        )
        assert "沒有開放預約" in reply or "沒有可預約" in reply
        assert any("2030-06-01" in b[0] for b in (buttons or []))

    def test_quota_exhausted_degrades(self, db, monkeypatch):
        monkeypatch.setattr(settings, "ai_allowance_base", 1)
        s = _seed(db)
        conv_svc.handle_free_text(db, s["tenant_id"], "Uai4", "剪髮")  # 用掉 1
        out = conv_svc.handle_free_text(db, s["tenant_id"], "Uai4", "還想約")
        reply, buttons = out
        assert "額度" in reply
        assert ("開始預約", "action=book") in buttons

    def test_usage_consumed_per_reply(self, db):
        s = _seed(db)
        conv_svc.handle_free_text(db, s["tenant_id"], "Uai5", "剪髮")
        assert ai_quota_svc.get_usage(db, s["tenant_id"]) == 1

    def test_ttl_expiry_resets_slots(self, db):
        s = _seed(db)
        conv_svc.handle_free_text(db, s["tenant_id"], "Uai6", "剪髮")
        conv = db.execute(
            select(LineConversation).where(LineConversation.line_user_id == "Uai6")
        ).scalar_one()
        conv.expires_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)
        db.commit()
        conv_svc.handle_free_text(db, s["tenant_id"], "Uai6", "3位")  # 有意圖才接手
        db.refresh(conv)
        assert "service_id" not in conv.slots  # 過期重置


# ── webhook 端到端（StubAgent：無 API key 時 get_agent 自動選用）──────────────

class TestWebhookEndToEnd:
    @pytest.fixture()
    def client(self):
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

    def _post(self, c, tid, event):
        body = json.dumps({"destination": "x", "events": [event]}).encode()
        mac = hmac.new(_CHANNEL_SECRET.encode(), body, hashlib.sha256)
        sig = base64.b64encode(mac.digest()).decode()
        r = c.post(
            f"/line/webhook/{tid}", content=body,
            headers={"X-Line-Signature": sig, "Content-Type": "application/json"},
        )
        assert r.status_code == 200

    def test_free_text_to_booking(self, client):
        c, line = client
        db = _Session()
        s = _seed(db)
        db.close()

        # 自然語言 → AI 補槽 → 時段按鈕
        self._post(c, s["tenant_id"], {
            "type": "message", "replyToken": "rt", "webhookEventId": "ai-e1",
            "source": {"type": "user", "userId": "Ue2e"},
            "message": {"type": "text", "text": "我要約 2030-06-01 剪髮 2位"},
        })
        qr = line.sent[-1].quick_reply or []
        tuples = [i for i in qr if not isinstance(i, dict)]
        slot_data = next(d for _l, d in tuples if "action=pick_slot" in d)

        # 點時段按鈕 → 既有確定性路徑建單
        self._post(c, s["tenant_id"], {
            "type": "postback", "replyToken": "rt2", "webhookEventId": "ai-e2",
            "source": {"type": "user", "userId": "Ue2e"},
            "postback": {"data": slot_data},
        })
        db = _Session()
        try:
            resv = db.execute(
                select(Reservation).where(
                    Reservation.tenant_id == s["tenant_id"]
                )
            ).scalar_one()
            assert resv.party_size == 2
            assert resv.service_id == s["service_id"]
        finally:
            db.close()

    def test_command_still_takes_priority(self, client):
        """既有指令不經 AI（action 非 None 直接走 dispatcher）。"""
        c, line = client
        db = _Session()
        s = _seed(db)
        db.close()
        self._post(c, s["tenant_id"], {
            "type": "message", "replyToken": "rt", "webhookEventId": "ai-e3",
            "source": {"type": "user", "userId": "Ucmd"},
            "message": {"type": "text", "text": "我的預約"},
        })
        assert "沒有預約" in line.sent[-1].text
        db = _Session()
        try:
            assert ai_quota_svc.get_usage(db, s["tenant_id"]) == 0  # 未耗 AI 額度
        finally:
            db.close()


# ── /quota/ai ────────────────────────────────────────────────────────────────

def test_quota_ai_endpoint(db):
    app = create_app()

    def override_db():
        s = _Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as c:
        email = f"q_{uuid.uuid4().hex[:6]}@x.tw"
        r = c.post("/auth/register", json={
            "email": email, "password": "Test1234!",
            "tenant_name": f"qshop_{uuid.uuid4().hex[:6]}",
        })
        token = r.json()["access_token"]
        r = c.get("/quota/ai", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        body = r.json()
        assert body["allowance"] == settings.ai_allowance_base
        assert body["used"] == 0 and body["boost_enabled"] is False
