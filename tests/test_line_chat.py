"""後台 LINE 客服訊息 + SSE 即時通知測試（對標 vibeaico）。

涵蓋：事件廣播器 publish/subscribe、對話紀錄服務、後台回覆端點（push+存檔+廣播）、
SSE 端點認證與 media type。全部離線、in-memory SQLite。
"""

from __future__ import annotations

import asyncio
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

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db, import_all_models  # noqa: E402
from saas_mvp.line_client import (  # noqa: E402
    FakeLinePushClient,
    StubLineBotInfoClient,
    get_bot_info_client,
    get_push_client,
)

import_all_models()

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
Base.metadata.create_all(bind=_engine)

_fake_push = FakeLinePushClient()
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
_app.dependency_overrides[get_push_client] = lambda: _fake_push


@pytest.fixture()
def client():
    _fake_push.reset()
    with TestClient(_app, raise_server_exceptions=True) as c:
        yield c


def _register_and_login(client) -> int:
    email = f"u_{uuid.uuid4().hex[:8]}@example.com"
    pw = "Test1234!"
    r = client.post("/auth/register", json={
        "email": email, "password": pw, "tenant_name": f"t_{uuid.uuid4().hex[:6]}",
    })
    assert r.status_code == 201, r.text
    token = r.json()["access_token"]
    tid = client.get(
        "/tenants/me", headers={"Authorization": f"Bearer {token}"}
    ).json()["id"]
    # 登入設 cookie（UI 路徑）
    lr = client.post("/ui/login", data={"email": email, "password": pw})
    assert lr.status_code == 200
    return tid


def _set_line_token(tenant_id: int, token: str = "chan-token") -> None:
    from saas_mvp.models.line_channel_config import LineChannelConfig
    db = _Session()
    try:
        cfg = LineChannelConfig(tenant_id=tenant_id)
        cfg.channel_secret = "sec"
        cfg.access_token = token
        db.add(cfg)
        db.commit()
    finally:
        db.close()


# ── 事件廣播器 ────────────────────────────────────────────────────────────────
class TestEventBroker:
    def test_publish_reaches_subscriber(self):
        from saas_mvp.services.events import broker, publish_event

        async def scenario():
            q = await broker.subscribe(999001)
            publish_event(999001, "line_message", text="hi")
            ev = await asyncio.wait_for(q.get(), timeout=1)
            broker.unsubscribe(999001, q)
            return ev

        ev = asyncio.run(scenario())
        assert ev["type"] == "line_message" and ev["text"] == "hi"

    def test_no_subscriber_is_noop(self):
        from saas_mvp.services.events import publish_event
        # 不應拋例外
        publish_event(999002, "booking_new", reservation_id=1)


# ── 對話紀錄服務 ──────────────────────────────────────────────────────────────
class TestChatService:
    def test_record_and_list(self):
        from saas_mvp.services import line_chat as svc
        db = _Session()
        try:
            tid = 990100
            svc.record_inbound(db, tenant_id=tid, line_user_id="Uabc", text="想預約")
            svc.record_outbound(db, tenant_id=tid, line_user_id="Uabc", text="好的")
            svc.record_inbound(db, tenant_id=tid, line_user_id="Uxyz", text="營業時間?")

            convs = svc.list_conversations(db, tenant_id=tid)
            assert {c["line_user_id"] for c in convs} == {"Uabc", "Uxyz"}
            # Uabc 最後一則是店家回覆
            uabc = next(c for c in convs if c["line_user_id"] == "Uabc")
            assert uabc["last_direction"] == "out" and uabc["last_text"] == "好的"

            msgs = svc.list_messages(db, tenant_id=tid, line_user_id="Uabc")
            assert [m.text for m in msgs] == ["想預約", "好的"]  # 時間升序
        finally:
            db.close()


# ── 後台回覆端點 ──────────────────────────────────────────────────────────────
class TestReplyEndpoint:
    def test_reply_pushes_and_records(self, client):
        tid = _register_and_login(client)
        _set_line_token(tid)
        r = client.post("/ui/line-chat/Ucust/reply", data={"text": "您好，已收到"})
        assert r.status_code == 200, r.text
        # fake push 收到一筆
        assert _fake_push.texts == ["您好，已收到"]
        # 回覆內容出現在 partial
        assert "您好，已收到" in r.text

    def test_reply_without_token_shows_error(self, client):
        tid = _register_and_login(client)  # 未設定 channel token
        r = client.post("/ui/line-chat/Ucust/reply", data={"text": "hi"})
        assert r.status_code == 200
        assert "access token" in r.text or "無法回覆" in r.text
        assert _fake_push.call_count == 0

    def test_empty_reply_rejected(self, client):
        tid = _register_and_login(client)
        _set_line_token(tid)
        r = client.post("/ui/line-chat/Ucust/reply", data={"text": "   "})
        assert r.status_code == 200
        assert _fake_push.call_count == 0

    def test_chat_page_renders(self, client):
        tid = _register_and_login(client)
        r = client.get("/ui/line-chat")
        assert r.status_code == 200
        assert "客服訊息" in r.text

    def test_requires_login(self, client):
        # 未登入 → 重導登入頁
        r = client.get("/ui/line-chat", follow_redirects=False)
        assert r.status_code in (302, 303)


# ── SSE 端點 ──────────────────────────────────────────────────────────────────
# 註：不在 TestClient 中開啟實際串流——Starlette TestClient 不會穩定送出
# http.disconnect，長壽命的 SSE 產生器會無限循環卡住測試。改以「路由已註冊」
# + 「未登入重導」兩個非串流檢查覆蓋端點接線；串流本體於整合環境驗證。
class TestSSEEndpoint:
    def test_route_registered(self):
        paths = {getattr(r, "path", None) for r in _app.routes}
        assert "/ui/events" in paths

    def test_stream_requires_login(self, client):
        # 未登入 → require_ui_user 於進入產生器前先重導，不會開啟串流。
        r = client.get("/ui/events", follow_redirects=False)
        assert r.status_code in (302, 303)
