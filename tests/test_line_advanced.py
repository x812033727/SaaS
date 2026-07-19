"""LINE 對話進階:中文容錯解析、留電話(PII 接線)、依服務時長過濾時段。

驗收標準
--------
- 容錯:「預約明天」→ book、「我要改期 7」→ reschedule、「取消7」→ cancel、
  「取消訂閱」→ None(高風險防誤觸)、「我的預約清單」→ my、slash 打錯 → None
- 留電話:PRIVACY_MODE 開 → 回 PII 表單連結(PiiRequest 落列);關 → 引導文案
- 時長過濾:60 分服務排除 30 分 slot;slot_end NULL 舊資料放行
- UI 建時段:時長換算 slot_end;非法時長回錯誤
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
import saas_mvp.models.pii_request as _pii  # noqa: F401,E402
import saas_mvp.models.service_category as _sc  # noqa: F401,E402
import saas_mvp.models.service as _svc  # noqa: F401,E402
import saas_mvp.models.service_staff as _ss  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.booking.commands import parse_booking_command  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.line_client import FakeLineReplyClient, get_line_client  # noqa: E402
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.line_channel_config import LineChannelConfig  # noqa: E402
from saas_mvp.models.pii_request import PiiRequest  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.services import catalog as catalog_svc  # noqa: E402
from saas_mvp.services import features as features_svc  # noqa: E402
from saas_mvp.translation import get_translator  # noqa: E402
from saas_mvp.translation.stub import StubTranslator  # noqa: E402

_CHANNEL_SECRET = "advanced_secret_value_0123456789ab"
_ACCESS_TOKEN = "advanced_access_token_value"

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


# ── 容錯解析(純函式) ─────────────────────────────────────────────────────────


class TestFuzzyParsing:
    @pytest.mark.parametrize("text,expected_action,expected_params", [
        ("預約明天", "book", {"party_size": 1}),
        ("預約12 3", "book", {"slot_id": 12, "party_size": 3}),
        ("我要改期 7", "reschedule", {"reservation_id": 7}),
        ("我想改期7", "reschedule", {"reservation_id": 7}),
        ("取消7", "cancel", {"reservation_id": 7}),
        ("我的預約清單", "my", {}),
        ("查詢時段表", "slots", {}),
        ("請幫我留電話", "contact", {}),
    ])
    def test_fuzzy_hits(self, text, expected_action, expected_params):
        action, params = parse_booking_command(text)
        assert action == expected_action
        assert params == expected_params

    @pytest.mark.parametrize("text", [
        "取消訂閱",       # 高風險 remainder 非數字 → 防誤觸
        "改期規則是什麼",  # 高風險 remainder 非數字
        "/bok",           # slash 打錯不 fuzzy
        "隨便聊天",
        "我覺得這時段不錯 但我沒有要預約",  # head 非指令,不掃全句
    ])
    def test_fuzzy_misses(self, text):
        action, _params = parse_booking_command(text)
        assert action is None

    def test_exact_match_still_first(self):
        # 精確比對優先,行為不變
        assert parse_booking_command("我的預約") == ("my", {})
        assert parse_booking_command("取消 7") == ("cancel", {"reservation_id": 7})


# ── webhook 整合(留電話 / 時長過濾) ─────────────────────────────────────────


@pytest.fixture()
def app_client():
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


def _seed(*, privacy: bool | None = None) -> int:
    db = _Session()
    try:
        t = Tenant(name=f"adv_{os.urandom(3).hex()}", plan="free")
        db.add(t)
        db.flush()
        cfg = LineChannelConfig(tenant_id=t.id, default_target_lang="zh-TW")
        cfg.channel_secret = _CHANNEL_SECRET
        cfg.access_token = _ACCESS_TOKEN
        cfg.bot_mode = "booking"
        db.add(cfg)
        if privacy is not None:
            # 明確設定旗標(features_default_enabled=True 的 dev 預設下,
            # 「未設定」會被視為開啟,測關閉行為必須顯式寫 False)。
            features_svc.set_enabled(
                db, t.id, features_svc.PRIVACY_MODE, privacy,
                actor_user_id=None, source="admin",
            )
        db.commit()
        return t.id
    finally:
        db.close()


_EID_SEQ = iter(range(10_000))


def _post_text(client, tenant_id: int, text: str, *, user="Uadv") -> None:
    event = {
        "type": "message",
        "replyToken": "rtok",
        "source": {"type": "user", "userId": user},
        "message": {"type": "text", "text": text},
        "webhookEventId": f"adv-{next(_EID_SEQ)}",
    }
    body = json.dumps({"destination": "x", "events": [event]}).encode()
    mac = hmac.new(_CHANNEL_SECRET.encode(), body, hashlib.sha256)
    sig = base64.b64encode(mac.digest()).decode()
    r = client.post(
        f"/line/webhook/{tenant_id}",
        content=body,
        headers={"X-Line-Signature": sig, "Content-Type": "application/json"},
    )
    assert r.status_code == 200, r.text


def _post_postback(client, tenant_id: int, data: str, *, user="Uadv") -> None:
    event = {
        "type": "postback",
        "replyToken": "rtok",
        "source": {"type": "user", "userId": user},
        "postback": {"data": data},
        "webhookEventId": f"adv-{next(_EID_SEQ)}",
    }
    body = json.dumps({"destination": "x", "events": [event]}).encode()
    mac = hmac.new(_CHANNEL_SECRET.encode(), body, hashlib.sha256)
    sig = base64.b64encode(mac.digest()).decode()
    r = client.post(
        f"/line/webhook/{tenant_id}",
        content=body,
        headers={"X-Line-Signature": sig, "Content-Type": "application/json"},
    )
    assert r.status_code == 200, r.text


class TestContactFlow:
    def test_privacy_on_returns_form_link(self, app_client, monkeypatch):
        from saas_mvp.config import settings

        monkeypatch.setattr(settings, "public_base_url", "https://shop.example")
        client, line = app_client
        tid = _seed(privacy=True)
        _post_text(client, tid, "留電話")
        reply = line.sent[-1].text
        assert "/pii/" in reply
        db = _Session()
        try:
            reqs = list(db.execute(
                select(PiiRequest).where(PiiRequest.tenant_id == tid)
            ).scalars())
            assert len(reqs) == 1
            assert reqs[0].line_user_id == "Uadv"
        finally:
            db.close()

    def test_privacy_off_returns_guidance(self, app_client):
        client, line = app_client
        tid = _seed(privacy=False)
        _post_text(client, tid, "留電話")
        assert "未開放" in line.sent[-1].text
        db = _Session()
        try:
            assert db.query(PiiRequest).count() == 0
        finally:
            db.close()


class TestDurationFilter:
    def _seed_service_and_slots(self, tid: int) -> tuple[int, int, int, int]:
        """建 60 分服務 + 三個 slot(30 分/90 分/無 end),回傳 id 們。"""
        db = _Session()
        try:
            svc = catalog_svc.create_service(
                db, tenant_id=tid, name="剪髮", duration_minutes=60
            )
            base = datetime.datetime(2030, 6, 1, 10, 0, tzinfo=datetime.timezone.utc)
            short = BookingSlot(
                tenant_id=tid, slot_start=base,
                slot_end=base + datetime.timedelta(minutes=30), max_capacity=4,
            )
            long = BookingSlot(
                tenant_id=tid, slot_start=base + datetime.timedelta(hours=2),
                slot_end=base + datetime.timedelta(hours=3, minutes=30),
                max_capacity=4,
            )
            legacy = BookingSlot(  # slot_end NULL → 放行
                tenant_id=tid, slot_start=base + datetime.timedelta(hours=5),
                max_capacity=4,
            )
            db.add_all([short, long, legacy])
            db.commit()
            return svc.id, short.id, long.id, legacy.id
        finally:
            db.close()

    def test_short_slot_filtered_legacy_passes(self, app_client):
        client, line = app_client
        tid = _seed()
        svc_id, short_id, long_id, legacy_id = self._seed_service_and_slots(tid)
        # 引導流程:選服務+日期+不指定員工 → 列時段
        _post_postback(
            client, tid, f"action=pick_staff&service_id={svc_id}&date=2030-06-01"
        )
        qr = line.sent[-1].quick_reply
        assert qr, line.sent[-1].text
        datas = [d for _, d in qr]
        assert not any(f"slot_id={short_id}" in d for d in datas)  # 30 分被濾
        assert any(f"slot_id={long_id}" in d for d in datas)       # 90 分保留
        assert any(f"slot_id={legacy_id}" in d for d in datas)     # NULL 放行

