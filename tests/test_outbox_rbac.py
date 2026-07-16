"""A0.2 webhook outbox + B5 店內 RBAC 測試。"""

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

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.line_client import (  # noqa: E402
    FakeLineReplyClient,
    StubLineProfileClient,
    get_line_client,
)
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.line_channel_config import LineChannelConfig  # noqa: E402
from saas_mvp.models.line_webhook_event import (  # noqa: E402
    LineWebhookEvent,
    LineWebhookEventStatus,
)
from saas_mvp.models.reservation import Reservation  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.models.user import User  # noqa: E402
from saas_mvp.ops.retry_stuck_webhook_events import retry_stuck_events  # noqa: E402
from saas_mvp.services import booking as booking_svc  # noqa: E402
from saas_mvp.translation import get_translator  # noqa: E402
from saas_mvp.translation.stub import StubTranslator  # noqa: E402

_CHANNEL_SECRET = "ob_secret_value_0123456789abcdefghij"

_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_NOW = datetime.datetime.now(datetime.timezone.utc)


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


def _seed_booking_tenant() -> dict:
    db = _Session()
    try:
        t = Tenant(name=f"ob_{uuid.uuid4().hex[:8]}", plan="pro")
        db.add(t)
        db.flush()
        cfg = LineChannelConfig(tenant_id=t.id, default_target_lang="zh-TW")
        cfg.channel_secret = _CHANNEL_SECRET
        cfg.access_token = "tok"
        cfg.bot_mode = "booking"
        db.add(cfg)
        slot = BookingSlot(
            tenant_id=t.id,
            slot_start=datetime.datetime(2030, 6, 1, 18, 0, tzinfo=datetime.timezone.utc),
            max_capacity=4,
        )
        db.add(slot)
        db.flush()
        out = {"tenant_id": t.id, "slot_id": slot.id}
        db.commit()
        return out
    finally:
        db.close()


def _post_webhook(c, tid, event):
    body = json.dumps({"destination": "x", "events": [event]}).encode()
    mac = hmac.new(_CHANNEL_SECRET.encode(), body, hashlib.sha256)
    sig = base64.b64encode(mac.digest()).decode()
    r = c.post(
        f"/line/webhook/{tid}", content=body,
        headers={"X-Line-Signature": sig, "Content-Type": "application/json"},
    )
    assert r.status_code == 200


# ── A0.2 outbox ──────────────────────────────────────────────────────────────

class TestOutbox:
    def test_event_is_persisted_before_background_dispatch(self, client, monkeypatch):
        """Worker 若在 response 後、background 開始前死掉，pending 仍已落盤。"""
        c, _ = client
        s = _seed_booking_tenant()

        async def skip_background(_self):
            return None

        monkeypatch.setattr(
            "starlette.background.BackgroundTasks.__call__", skip_background
        )
        _post_webhook(c, s["tenant_id"], {
            "type": "message", "replyToken": "rt", "webhookEventId": "ob-durable",
            "source": {"type": "user", "userId": "Udurable"},
            "message": {"type": "text", "text": "時段"},
        })

        with _Session() as db:
            row = db.execute(
                select(LineWebhookEvent).where(
                    LineWebhookEvent.webhook_event_id == "ob-durable"
                )
            ).scalar_one()
            assert row.status == LineWebhookEventStatus.PENDING.value
            assert json.loads(row.payload_json)["message"]["text"] == "時段"

    def test_payload_persisted_on_claim(self, client):
        c, _ = client
        s = _seed_booking_tenant()
        _post_webhook(c, s["tenant_id"], {
            "type": "message", "replyToken": "rt", "webhookEventId": "ob-e1",
            "source": {"type": "user", "userId": "Uob1"},
            "message": {"type": "text", "text": "時段"},
        })
        db = _Session()
        try:
            row = db.execute(
                select(LineWebhookEvent).where(
                    LineWebhookEvent.webhook_event_id == "ob-e1"
                )
            ).scalar_one()
            assert row.payload_json
            assert json.loads(row.payload_json)["message"]["text"] == "時段"
        finally:
            db.close()

    def test_stuck_pending_replayed_and_side_effect_applied(self, client):
        """模擬 worker 死在處理中：pending + payload 落盤 → 重放補齊建單。"""
        _c, _ = client
        s = _seed_booking_tenant()
        event = {
            "type": "message", "replyToken": "rt-dead", "webhookEventId": "ob-e2",
            "source": {"type": "user", "userId": "Uob2"},
            "message": {"type": "text", "text": f"預約 {s['slot_id']} 2"},
        }
        db = _Session()
        try:
            db.add(LineWebhookEvent(
                tenant_id=s["tenant_id"],
                webhook_event_id="ob-e2",
                status=LineWebhookEventStatus.PENDING.value,
                last_stage="claimed",
                payload_json=json.dumps(event, ensure_ascii=False),
                updated_at=_NOW - datetime.timedelta(minutes=30),
            ))
            db.commit()
        finally:
            db.close()

        factory = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
        fake_reply = FakeLineReplyClient()
        results = retry_stuck_events(
            session_factory=factory, apply=True, now=_NOW,
            line_client=fake_reply,
            profile_client=StubLineProfileClient(display_name="補救客"),
            translator=StubTranslator(),
        )
        assert [r.status for r in results] == ["processed"]

        db = _Session()
        try:
            resv = db.execute(
                select(Reservation).where(Reservation.tenant_id == s["tenant_id"])
            ).scalar_one()
            assert resv.party_size == 2  # 建單被補齊 — 重放的核心價值
            row = db.execute(
                select(LineWebhookEvent).where(
                    LineWebhookEvent.webhook_event_id == "ob-e2"
                )
            ).scalar_one()
            assert row.status == LineWebhookEventStatus.PROCESSED.value
        finally:
            db.close()

    def test_fresh_pending_not_replayed(self, client):
        _c, _ = client
        s = _seed_booking_tenant()
        db = _Session()
        try:
            db.add(LineWebhookEvent(
                tenant_id=s["tenant_id"],
                webhook_event_id="ob-e3",
                status=LineWebhookEventStatus.PENDING.value,
                last_stage="claimed",
                payload_json="{}",
                updated_at=_NOW,  # 剛入列,不算卡住
            ))
            db.commit()
        finally:
            db.close()
        factory = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
        assert retry_stuck_events(
            session_factory=factory, apply=True, now=_NOW,
            line_client=FakeLineReplyClient(), translator=StubTranslator(),
        ) == []


# ── A0.2 重放冪等 + 原子認領 ───────────────────────────────────────────────────

class TestReplayIdempotency:
    def test_book_slot_idempotent_by_webhook_event(self, client):
        """同一 source_webhook_event_id 重複 book_slot → 回同一筆、只佔一個名額。"""
        _c, _ = client
        s = _seed_booking_tenant()
        db = _Session()
        try:
            r1 = booking_svc.book_slot(
                db, tenant_id=s["tenant_id"], slot_id=s["slot_id"], party_size=1,
                line_user_id="Uidem2", source_webhook_event_id="whe-1",
            )
            r2 = booking_svc.book_slot(
                db, tenant_id=s["tenant_id"], slot_id=s["slot_id"], party_size=1,
                line_user_id="Uidem2", source_webhook_event_id="whe-1",
            )
            assert r1.id == r2.id
            resvs = db.execute(
                select(Reservation).where(Reservation.tenant_id == s["tenant_id"])
            ).scalars().all()
            assert len(resvs) == 1
            assert db.get(BookingSlot, s["slot_id"]).booked_count == 1
        finally:
            db.close()

    def test_replay_does_not_double_book(self, client):
        """#1 修復:原始處理已建單(掛 source_webhook_event_id)後崩潰,event 停 pending;
        重放走同一 webhook 事件 → book_slot 冪等回既有預約,不建第二筆。"""
        _c, _ = client
        s = _seed_booking_tenant()
        event = {
            "type": "message", "replyToken": "rt", "webhookEventId": "ob-idem",
            "source": {"type": "user", "userId": "Uidem"},
            "message": {"type": "text", "text": f"預約 {s['slot_id']} 2"},
        }
        db = _Session()
        try:
            db.add(Reservation(
                tenant_id=s["tenant_id"], slot_id=s["slot_id"], party_size=2,
                status="confirmed", line_user_id="Uidem",
                source_webhook_event_id="ob-idem",
            ))
            db.add(LineWebhookEvent(
                tenant_id=s["tenant_id"], webhook_event_id="ob-idem",
                status=LineWebhookEventStatus.PENDING.value, last_stage="claimed",
                payload_json=json.dumps(event, ensure_ascii=False),
                updated_at=_NOW - datetime.timedelta(minutes=30),
            ))
            db.commit()
        finally:
            db.close()

        factory = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
        retry_stuck_events(
            session_factory=factory, apply=True, now=_NOW,
            line_client=FakeLineReplyClient(),
            profile_client=StubLineProfileClient(display_name="x"),
            translator=StubTranslator(),
        )
        db = _Session()
        try:
            resvs = db.execute(
                select(Reservation).where(Reservation.tenant_id == s["tenant_id"])
            ).scalars().all()
            assert len(resvs) == 1  # 冪等:沒有重複建單
            assert resvs[0].source_webhook_event_id == "ob-idem"
        finally:
            db.close()

    def test_stuck_processing_reclaimed(self, client):
        """#2 修復:認領後崩潰(卡住的 PROCESSING)於下輪被重新認領並重放。"""
        _c, _ = client
        s = _seed_booking_tenant()
        event = {
            "type": "message", "replyToken": "rt", "webhookEventId": "ob-proc",
            "source": {"type": "user", "userId": "Uproc"},
            "message": {"type": "text", "text": f"預約 {s['slot_id']} 1"},
        }
        db = _Session()
        try:
            db.add(LineWebhookEvent(
                tenant_id=s["tenant_id"], webhook_event_id="ob-proc",
                status=LineWebhookEventStatus.PROCESSING.value,
                last_stage="claimed", attempt_count=1,
                payload_json=json.dumps(event, ensure_ascii=False),
                updated_at=_NOW - datetime.timedelta(minutes=30),
            ))
            db.commit()
        finally:
            db.close()
        factory = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
        results = retry_stuck_events(
            session_factory=factory, apply=True, now=_NOW,
            line_client=FakeLineReplyClient(),
            profile_client=StubLineProfileClient(display_name="x"),
            translator=StubTranslator(),
        )
        assert [r.status for r in results] == ["processed"]
        db = _Session()
        try:
            row = db.execute(select(LineWebhookEvent).where(
                LineWebhookEvent.webhook_event_id == "ob-proc")).scalar_one()
            assert row.status == LineWebhookEventStatus.PROCESSED.value
            assert row.attempt_count == 2  # 認領 +1
        finally:
            db.close()

    def test_fresh_processing_not_reclaimed(self, client):
        """剛認領(updated_at 新)的 PROCESSING 代表別的實例正在處理,不得搶。"""
        _c, _ = client
        s = _seed_booking_tenant()
        db = _Session()
        try:
            db.add(LineWebhookEvent(
                tenant_id=s["tenant_id"], webhook_event_id="ob-proc-fresh",
                status=LineWebhookEventStatus.PROCESSING.value,
                last_stage="claimed", payload_json="{}",
                updated_at=_NOW,
            ))
            db.commit()
        finally:
            db.close()
        factory = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
        assert retry_stuck_events(
            session_factory=factory, apply=True, now=_NOW,
            line_client=FakeLineReplyClient(), translator=StubTranslator(),
        ) == []


# ── B5 RBAC ──────────────────────────────────────────────────────────────────

def _register_owner(c) -> tuple[str, int]:
    email = f"own_{uuid.uuid4().hex[:8]}@x.tw"
    c.post("/auth/register", json={
        "email": email, "password": "Test1234!",
        "tenant_name": f"rb_{uuid.uuid4().hex[:8]}",
    })
    c.post("/ui/login", data={"email": email, "password": "Test1234!"})
    db = _Session()
    try:
        u = db.query(User).filter(User.email == email).one()
        return email, u.tenant_id
    finally:
        db.close()


class TestRBAC:
    def test_owner_can_open_billing(self, client):
        c, _ = client
        _register_owner(c)
        assert c.get("/ui/billing").status_code == 200

    def test_invite_flow_creates_staff(self, client):
        c, _ = client
        _, tid = _register_owner(c)
        r = c.post("/ui/members/invite")
        assert r.status_code == 200
        token = r.text.split("/ui/join/")[1].split("<")[0].strip()

        # 受邀者以乾淨 client 加入
        c.get("/ui/logout")
        staff_email = f"st_{uuid.uuid4().hex[:8]}@x.tw"
        r = c.post(
            f"/ui/join/{token}",
            data={"email": staff_email, "password": "longpassword"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        db = _Session()
        try:
            staff = db.query(User).filter(User.email == staff_email).one()
            assert staff.role == "staff" and staff.tenant_id == tid
        finally:
            db.close()

        # staff 進帳務/方案/LINE 設定 → 403;日常頁可用
        assert c.get("/ui/billing").status_code == 403
        assert c.get("/ui/plan").status_code == 403
        assert c.get("/ui/line-config").status_code == 403
        assert c.get("/ui/members").status_code == 403
        assert c.get("/ui/booking").status_code == 200
        assert c.get("/ui/customers").status_code == 200
        # 員工僱傭管理與單點訂閱(帳務)也限 owner;檢視/排班留給 staff
        assert c.post("/ui/staff", data={"name": "小王"}).status_code == 403
        assert c.post("/ui/features/COUPON_SYSTEM/subscribe").status_code == 403
        assert c.post("/ui/features/COUPON_SYSTEM/unsubscribe").status_code == 403
        assert c.get("/ui/staff").status_code == 200   # 檢視/排班開放
        assert c.get("/ui/features").status_code == 200  # 檢視開放

        # 邀請連結一次性
        c.get("/ui/logout")
        r = c.post(
            f"/ui/join/{token}",
            data={"email": f"x_{uuid.uuid4().hex[:6]}@x.tw", "password": "longpassword"},
        )
        assert r.status_code == 400

    def test_join_with_bad_token_rejected(self, client):
        c, _ = client
        r = c.post(
            "/ui/join/garbage",
            data={"email": "g@x.tw", "password": "longpassword"},
        )
        assert r.status_code == 400
