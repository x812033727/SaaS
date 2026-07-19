"""顧客黑名單 — 硬性阻擋線上預約（service / LINE webhook / REST / 後台 UI）。

驗收：
  - book_slot 對黑名單 LINE 顧客早退拋 CustomerBlacklistedError，不佔名額
  - LINE 預約被擋 → 回中性婉拒訊息、不建預約；未列黑名單者照常成功
  - REST POST /booking/reservations → 403；/booking/customers/{id}/blacklist 設定/解除
  - 解除黑名單一併清空 reason
  - 後台 UI toggle 後顧客卡片顯示/移除「黑名單」徽章
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
    FakeLineReplyClient,
    StubLineProfileClient,
    get_line_client,
    get_profile_client,
)
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.customer import Customer  # noqa: E402
from saas_mvp.models.line_channel_config import LineChannelConfig  # noqa: E402
from saas_mvp.models.reservation import (  # noqa: E402
    RESERVATION_CONFIRMED,
    Reservation,
)
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.services import booking as booking_svc  # noqa: E402
from saas_mvp.services import customers as customers_svc  # noqa: E402
from saas_mvp.translation import get_translator  # noqa: E402
from saas_mvp.translation.stub import StubTranslator  # noqa: E402

_CHANNEL_SECRET = "blk_secret_value_0123456789abcdef"
_ACCESS_TOKEN = "blk_access_token_value"
_USER = "U" + "e" * 32

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    yield


def _build_client() -> tuple[TestClient, FakeLineReplyClient]:
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
    app.dependency_overrides[get_profile_client] = lambda: StubLineProfileClient(
        display_name="Ian"
    )
    return TestClient(app, raise_server_exceptions=True), line_client


def _seed(*, blacklisted=False, reason=None) -> dict:
    """建 tenant + booking 模式 LINE config + 一個時段；可選預先建黑名單顧客。"""
    db = _Session()
    try:
        t = Tenant(name=f"blk_{os.urandom(3).hex()}", plan="free")
        db.add(t)
        db.flush()
        cfg = LineChannelConfig(tenant_id=t.id, default_target_lang="zh-TW")
        cfg.channel_secret = _CHANNEL_SECRET
        cfg.access_token = _ACCESS_TOKEN
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
        if blacklisted:
            cust = Customer(
                tenant_id=t.id,
                line_user_id=_USER,
                display_name="壞客人",
                blacklisted=True,
                blacklist_reason=reason,
            )
            db.add(cust)
            db.flush()
            out["customer_id"] = cust.id
        db.commit()
        return out
    finally:
        db.close()


def _text_event(text, *, eid="e") -> dict:
    return {
        "type": "message",
        "replyToken": "rt",
        "source": {"type": "user", "userId": _USER},
        "message": {"type": "text", "text": text},
        "webhookEventId": eid,
    }


def _post_webhook(client, tenant_id, *events):
    body = json.dumps({"destination": "x", "events": list(events)}).encode()
    sig = base64.b64encode(
        hmac.new(_CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    ).decode()
    r = client.post(
        f"/line/webhook/{tenant_id}",
        content=body,
        headers={"X-Line-Signature": sig, "Content-Type": "application/json"},
    )
    assert r.status_code == 200, r.text


def _confirmed_count(tenant_id) -> int:
    db = _Session()
    try:
        return len(
            db.execute(
                select(Reservation).where(
                    Reservation.tenant_id == tenant_id,
                    Reservation.status == RESERVATION_CONFIRMED,
                )
            ).scalars().all()
        )
    finally:
        db.close()


# ─────────────────────────── service 層 ──────────────────────────────────────

class TestServiceLayer:
    def test_book_slot_rejects_blacklisted(self):
        s = _seed(blacklisted=True, reason="多次爽約")
        db = _Session()
        try:
            with pytest.raises(booking_svc.CustomerBlacklistedError):
                booking_svc.book_slot(
                    db,
                    tenant_id=s["tenant_id"],
                    slot_id=s["slot_id"],
                    line_user_id=_USER,
                )
        finally:
            db.close()
        # 早退：不佔名額、不建預約
        assert _confirmed_count(s["tenant_id"]) == 0

    def test_book_slot_allows_non_blacklisted(self):
        s = _seed()
        db = _Session()
        try:
            resv = booking_svc.book_slot(
                db, tenant_id=s["tenant_id"], slot_id=s["slot_id"], line_user_id=_USER
            )
            assert resv.id is not None
        finally:
            db.close()
        assert _confirmed_count(s["tenant_id"]) == 1

    def test_set_blacklist_clears_reason_on_unset(self):
        s = _seed()
        db = _Session()
        try:
            cust = Customer(tenant_id=s["tenant_id"], line_user_id=_USER, display_name="A")
            db.add(cust)
            db.commit()
            cid = cust.id

            c1 = customers_svc.set_blacklist(
                db, tenant_id=s["tenant_id"], customer_id=cid,
                blacklisted=True, reason="爽約3次",
            )
            assert c1.blacklisted is True and c1.blacklist_reason == "爽約3次"

            c2 = customers_svc.set_blacklist(
                db, tenant_id=s["tenant_id"], customer_id=cid, blacklisted=False,
            )
            assert c2.blacklisted is False and c2.blacklist_reason is None
        finally:
            db.close()


# ─────────────────────────── LINE webhook ────────────────────────────────────

class TestLineWebhook:
    def test_blacklisted_booking_blocked(self):
        s = _seed(blacklisted=True, reason="爽約")
        client, lc = _build_client()
        _post_webhook(client, s["tenant_id"], _text_event(f"預約 {s['slot_id']} 1", eid="x1"))

        assert lc.last_text is not None and "無法在線上預約" in lc.last_text
        assert _confirmed_count(s["tenant_id"]) == 0

    def test_non_blacklisted_booking_succeeds(self):
        s = _seed()
        client, lc = _build_client()
        _post_webhook(client, s["tenant_id"], _text_event(f"預約 {s['slot_id']} 1", eid="x2"))

        assert lc.last_text is not None and "預約成功" in lc.last_text
        assert _confirmed_count(s["tenant_id"]) == 1


# ─────────────────────────── REST API ────────────────────────────────────────

def _register(client) -> tuple[str, str, str]:
    """回傳 (token, email, password)。"""
    email = f"u_{uuid.uuid4().hex[:8]}@example.com"
    password = "Test1234!"
    r = client.post("/auth/register", json={
        "email": email, "password": password, "tenant_name": f"t_{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 201, r.text
    return r.json()["access_token"], email, password


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestRest:
    def test_blacklist_endpoint_then_booking_forbidden(self):
        client, _ = _build_client()
        token, _e, _p = _register(client)
        slot = client.post(
            "/booking/slots/", headers=_auth(token),
            json={"slot_start": "2030-06-01T18:00:00+00:00", "max_capacity": 4},
        ).json()

        # 首次預約：建立顧客檔（非黑名單）
        r1 = client.post(
            "/booking/reservations/", headers=_auth(token),
            json={"slot_id": slot["id"], "line_user_id": _USER},
        )
        assert r1.status_code == 201, r1.text

        cust = client.get("/booking/customers/", headers=_auth(token)).json()[0]
        assert cust["blacklisted"] is False

        # 加入黑名單
        rb = client.post(
            f"/booking/customers/{cust['id']}/blacklist", headers=_auth(token),
            json={"blacklisted": True, "reason": "多次爽約"},
        )
        assert rb.status_code == 200
        assert rb.json()["blacklisted"] is True
        assert rb.json()["blacklist_reason"] == "多次爽約"

        # 再次預約 → 403
        r2 = client.post(
            "/booking/reservations/", headers=_auth(token),
            json={"slot_id": slot["id"], "line_user_id": _USER},
        )
        assert r2.status_code == 403, r2.text

    def test_unblacklist_via_endpoint_clears_reason(self):
        client, _ = _build_client()
        token, _e, _p = _register(client)
        slot = client.post(
            "/booking/slots/", headers=_auth(token),
            json={"slot_start": "2030-06-01T18:00:00+00:00", "max_capacity": 4},
        ).json()
        client.post(
            "/booking/reservations/", headers=_auth(token),
            json={"slot_id": slot["id"], "line_user_id": _USER},
        )
        cid = client.get("/booking/customers/", headers=_auth(token)).json()[0]["id"]
        client.post(
            f"/booking/customers/{cid}/blacklist", headers=_auth(token),
            json={"blacklisted": True, "reason": "x"},
        )
        r = client.post(
            f"/booking/customers/{cid}/blacklist", headers=_auth(token),
            json={"blacklisted": False},
        )
        assert r.status_code == 200
        assert r.json()["blacklisted"] is False
        assert r.json()["blacklist_reason"] is None


# ─────────────────────────── 後台 UI ─────────────────────────────────────────

class TestUi:
    def test_toggle_blacklist_renders_badge(self):
        client, _ = _build_client()
        token, email, password = _register(client)
        # 為該租戶建一個顧客
        slot = client.post(
            "/booking/slots/", headers=_auth(token),
            json={"slot_start": "2030-06-01T18:00:00+00:00", "max_capacity": 4},
        ).json()
        client.post(
            "/booking/reservations/", headers=_auth(token),
            json={"slot_id": slot["id"], "line_user_id": _USER},
        )
        cid = client.get("/booking/customers/", headers=_auth(token)).json()[0]["id"]

        # R12-C3a:/ui/booking 頁已刪,黑名單切換改驗 API 端點
        # (console 顧客明細頁走同一端點)。
        on = client.post(
            f"/booking/customers/{cid}/blacklist", headers=_auth(token),
            json={"blacklisted": True, "reason": "現場鬧事"},
        )
        assert on.status_code == 200
        body = on.json()
        assert body["blacklisted"] is True
        assert body.get("blacklist_reason") == "現場鬧事"

        off = client.post(
            f"/booking/customers/{cid}/blacklist", headers=_auth(token),
            json={"blacklisted": False},
        )
        assert off.status_code == 200
        assert off.json()["blacklisted"] is False

