"""LINE follow/unfollow 事件 + 歡迎訊息測試。

覆蓋:
- follow(booking 模式):顧客自動建檔(line_followed=True)+ 預設歡迎訊息 + 預約 quick-reply
- follow 自訂 welcome_message:用租戶自訂文案
- follow(translation 模式):翻譯版預設文案、無 quick-reply
- unfollow:顧客標記 line_followed=False;re-follow 翻回 True
- booking 模式非文字訊息(貼圖):友善引導 + quick-reply(不再落到說明文字牆)
- /ui/line-config/welcome 設定端點由 test_ui_* 風格覆蓋於 service 層(set_welcome_message)
"""

from __future__ import annotations

import base64
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
from saas_mvp.models import customer as _c  # noqa: F401,E402
import saas_mvp.models.line_channel_config as _lcm  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.line_client import FakeLineReplyClient, get_line_client  # noqa: E402
from saas_mvp.models.customer import Customer  # noqa: E402
from saas_mvp.models.line_channel_config import LineChannelConfig  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.services import line_config as line_config_svc  # noqa: E402
from saas_mvp.translation import get_translator  # noqa: E402
from saas_mvp.translation.stub import StubTranslator  # noqa: E402

_CHANNEL_SECRET = "follow_secret_value_0123456789abcdef"
_ACCESS_TOKEN = "follow_access_token_value"

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


def _seed(bot_mode: str, *, welcome_message: str | None = None) -> int:
    db = _Session()
    try:
        t = Tenant(name=f"fw_{bot_mode}_{os.urandom(3).hex()}", plan="free")
        db.add(t)
        db.flush()
        cfg = LineChannelConfig(tenant_id=t.id, default_target_lang="zh-TW")
        cfg.channel_secret = _CHANNEL_SECRET
        cfg.access_token = _ACCESS_TOKEN
        cfg.bot_mode = bot_mode
        cfg.welcome_message = welcome_message
        db.add(cfg)
        db.commit()
        return t.id
    finally:
        db.close()


def _follow_event(*, user="Ufollower", token="rtokf", eid="evtf1") -> dict:
    return {
        "type": "follow",
        "replyToken": token,
        "source": {"type": "user", "userId": user},
        "webhookEventId": eid,
    }


def _unfollow_event(*, user="Ufollower", eid="evtu1") -> dict:
    # unfollow 無 replyToken
    return {
        "type": "unfollow",
        "source": {"type": "user", "userId": user},
        "webhookEventId": eid,
    }


def _sticker_event(*, user="Usticker", token="rtoks", eid="evts1") -> dict:
    return {
        "type": "message",
        "replyToken": token,
        "source": {"type": "user", "userId": user},
        "message": {"type": "sticker", "packageId": "1", "stickerId": "2"},
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


def _customer(tenant_id: int, line_user_id: str) -> Customer | None:
    db = _Session()
    try:
        return db.execute(
            select(Customer).where(
                Customer.tenant_id == tenant_id,
                Customer.line_user_id == line_user_id,
            )
        ).scalar_one_or_none()
    finally:
        db.close()


class TestFollowEvent:
    def test_follow_booking_creates_customer_and_replies_welcome(self, app_client):
        client, line = app_client
        tid = _seed("booking")
        _post(client, tid, _follow_event(user="Unew1"))

        c = _customer(tid, "Unew1")
        assert c is not None
        assert c.line_followed is True
        assert c.line_followed_at is not None
        assert c.booking_count == 0  # follow 不算預約

        assert len(line.sent) == 1
        assert "感謝加入好友" in line.sent[0].text
        labels = [item[0] for item in (line.sent[0].quick_reply or [])]
        assert "開始預約" in labels

    def test_follow_uses_custom_welcome_message(self, app_client):
        client, line = app_client
        tid = _seed("booking", welcome_message="歡迎光臨美美沙龍!")
        _post(client, tid, _follow_event(user="Unew2"))
        assert line.sent[0].text == "歡迎光臨美美沙龍!"

    def test_follow_translation_mode_default_text_no_quick_reply(self, app_client):
        client, line = app_client
        tid = _seed("translation")
        _post(client, tid, _follow_event(user="Unew3"))
        assert "自動翻譯" in line.sent[0].text
        assert not line.sent[0].quick_reply

    def test_follow_idempotent_upsert(self, app_client):
        client, line = app_client
        tid = _seed("booking")
        _post(client, tid, _follow_event(user="Udup", eid="e1"))
        _post(client, tid, _follow_event(user="Udup", eid="e2"))
        db = _Session()
        try:
            rows = db.execute(
                select(Customer).where(Customer.tenant_id == tid)
            ).scalars().all()
            assert len(rows) == 1
        finally:
            db.close()


class TestUnfollowEvent:
    def test_unfollow_marks_customer_not_followed(self, app_client):
        client, _line = app_client
        tid = _seed("booking")
        _post(client, tid, _follow_event(user="Ubye", eid="e1"))
        _post(client, tid, _unfollow_event(user="Ubye", eid="e2"))
        c = _customer(tid, "Ubye")
        assert c is not None and c.line_followed is False

    def test_refollow_flips_back_to_followed(self, app_client):
        client, _line = app_client
        tid = _seed("booking")
        _post(client, tid, _follow_event(user="Uback", eid="e1"))
        _post(client, tid, _unfollow_event(user="Uback", eid="e2"))
        _post(client, tid, _follow_event(user="Uback", eid="e3"))
        c = _customer(tid, "Uback")
        assert c is not None and c.line_followed is True

    def test_unfollow_unknown_customer_noop(self, app_client):
        client, _line = app_client
        tid = _seed("booking")
        _post(client, tid, _unfollow_event(user="Ughost"))
        assert _customer(tid, "Ughost") is None  # 不無中生有建檔


class TestNonTextMessage:
    def test_sticker_in_booking_mode_gets_guidance_with_quick_reply(self, app_client):
        client, line = app_client
        tid = _seed("booking")
        _post(client, tid, _sticker_event())
        assert len(line.sent) == 1
        assert "預約" in line.sent[0].text
        labels = [item[0] for item in (line.sent[0].quick_reply or [])]
        assert "開始預約" in labels


class TestWelcomeMessageService:
    def test_set_welcome_message_roundtrip_and_clear(self, app_client):
        _client, _line = app_client
        tid = _seed("booking")
        db = _Session()
        try:
            resp = line_config_svc.set_welcome_message(db, tid, "  客製歡迎  ")
            assert resp["welcome_message"] == "客製歡迎"  # 前後空白正規化
            resp = line_config_svc.set_welcome_message(db, tid, "   ")
            assert resp["welcome_message"] is None  # 空白＝清空回預設
        finally:
            db.close()

    def test_set_welcome_message_too_long_rejected(self, app_client):
        _client, _line = app_client
        tid = _seed("booking")
        db = _Session()
        try:
            from fastapi import HTTPException

            with pytest.raises(HTTPException):
                line_config_svc.set_welcome_message(db, tid, "x" * 1001)
        finally:
            db.close()
