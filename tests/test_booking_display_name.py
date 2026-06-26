"""LINE 預約建單時回填顧客 display_name 的端到端行為。

驗收：
  - 建單動作（raw book / 引導式 pick_slot）會向 LINE profile 取名字寫進顧客檔
  - profile 抓取失敗 → 仍建單成功、display_name 留 None（非致命）
  - 既有顧客已有名字 → 不被覆蓋（保留店家手動編輯）
  - 非建單動作（我的預約）不呼叫 profile API
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
os.environ.setdefault(
    "SAAS_LINE_CHANNEL_ENCRYPT_KEY",
    "ZGV2LWxpbmUtc2VjcmV0LWtleS0zMmJ5dGVzLWxvbmc=",
)

from saas_mvp.models import tenant as _t, user as _u  # noqa: F401,E402
from saas_mvp.models import customer as _c, booking_slot as _bs  # noqa: F401,E402
from saas_mvp.models import reservation as _r, reservation_reminder as _rr  # noqa: F401,E402
import saas_mvp.models.line_channel_config as _lcm  # noqa: F401,E402
import saas_mvp.models.staff as _staff  # noqa: F401,E402
import saas_mvp.models.service as _svc  # noqa: F401,E402
import saas_mvp.models.service_staff as _ss  # noqa: F401,E402

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
from saas_mvp.models.reservation import Reservation  # noqa: E402
from saas_mvp.models.service import Service  # noqa: E402
from saas_mvp.models.service_staff import ServiceStaff  # noqa: E402
from saas_mvp.models.staff import Staff  # noqa: E402
from saas_mvp.models.line_channel_config import LineChannelConfig  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.translation import get_translator  # noqa: E402
from saas_mvp.translation.stub import StubTranslator  # noqa: E402

_CHANNEL_SECRET = "name_secret_value_0123456789abcdef"
_ACCESS_TOKEN = "name_access_token_value"
_USER = "U" + "d" * 32

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


def _build_client(profile_client) -> TestClient:
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
    app.dependency_overrides[get_profile_client] = lambda: profile_client
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    yield


def _seed(*, with_service=False) -> dict:
    db = _Session()
    try:
        t = Tenant(name=f"name_{os.urandom(3).hex()}", plan="free")
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
        if with_service:
            svc = Service(tenant_id=t.id, name="剪髮", duration_minutes=30, price_cents=500)
            db.add(svc)
            db.flush()
            st = Staff(tenant_id=t.id, name="小美")
            db.add(st)
            db.flush()
            db.add(ServiceStaff(tenant_id=t.id, service_id=svc.id, staff_id=st.id))
            out["service_id"] = svc.id
            out["staff_id"] = st.id
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


def _postback_event(data, *, eid="e") -> dict:
    return {
        "type": "postback",
        "replyToken": "rt",
        "source": {"type": "user", "userId": _USER},
        "postback": {"data": data},
        "webhookEventId": eid,
    }


def _post(client, tenant_id, *events):
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


def _customer(tenant_id):
    db = _Session()
    try:
        return db.execute(
            select(Customer).where(
                Customer.tenant_id == tenant_id, Customer.line_user_id == _USER
            )
        ).scalar_one_or_none()
    finally:
        db.close()


def _reservation_count(tenant_id) -> int:
    db = _Session()
    try:
        return len(
            db.execute(
                select(Reservation).where(Reservation.tenant_id == tenant_id)
            ).scalars().all()
        )
    finally:
        db.close()


def test_raw_book_backfills_display_name():
    s = _seed()
    client = _build_client(StubLineProfileClient(display_name="王小明"))
    _post(client, s["tenant_id"], _text_event(f"預約 {s['slot_id']} 2", eid="b1"))

    cust = _customer(s["tenant_id"])
    assert cust is not None
    assert cust.display_name == "王小明"


def test_conversational_pick_slot_backfills_display_name():
    s = _seed(with_service=True)
    client = _build_client(StubLineProfileClient(display_name="陳大文"))
    data = (
        f"action=pick_slot&service_id={s['service_id']}"
        f"&staff_id={s['staff_id']}&slot_id={s['slot_id']}"
    )
    _post(client, s["tenant_id"], _postback_event(data, eid="b2"))

    assert _reservation_count(s["tenant_id"]) == 1
    cust = _customer(s["tenant_id"])
    assert cust is not None and cust.display_name == "陳大文"


def test_profile_failure_is_non_fatal():
    s = _seed()
    client = _build_client(StubLineProfileClient(raises=True))
    _post(client, s["tenant_id"], _text_event(f"預約 {s['slot_id']} 1", eid="b3"))

    # 建單仍成功；名字留空（UI 以 line_user_id 兜底）。
    assert _reservation_count(s["tenant_id"]) == 1
    cust = _customer(s["tenant_id"])
    assert cust is not None and cust.display_name is None


def test_existing_name_not_overwritten():
    s = _seed()
    # 預先建立同一 LINE 顧客且已有店家編輯過的名字。
    db = _Session()
    try:
        db.add(Customer(
            tenant_id=s["tenant_id"],
            line_user_id=_USER,
            display_name="原本美容客",
        ))
        db.commit()
    finally:
        db.close()

    client = _build_client(StubLineProfileClient(display_name="LINE暱稱"))
    _post(client, s["tenant_id"], _text_event(f"預約 {s['slot_id']} 1", eid="b4"))

    cust = _customer(s["tenant_id"])
    assert cust is not None and cust.display_name == "原本美容客"


def test_non_create_action_skips_profile_fetch():
    s = _seed()
    stub = StubLineProfileClient(display_name="不該被呼叫")
    client = _build_client(stub)
    _post(client, s["tenant_id"], _text_event("我的預約", eid="b5"))

    assert stub.calls == []
