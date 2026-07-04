"""提醒訊息「確認出席 / 取消預約」互動。

驗收標準
--------
- migration：舊 booking_reservations 缺 customer_confirmed_at → 補欄（冪等）
- 提醒推播帶「確認出席 / 取消預約」quick reply（postback 走 confirm/cancel）
- confirm postback：寫入 customer_confirmed_at；重複確認冪等（保留首次時間）
- 他人的預約不可確認；已取消不可確認
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
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

import saas_mvp.db as dbmod  # noqa: E402

from saas_mvp.models import tenant as _t, user as _u  # noqa: F401,E402
from saas_mvp.models import customer as _c, booking_slot as _bs  # noqa: F401,E402
from saas_mvp.models import reservation as _r, reservation_reminder as _rr  # noqa: F401,E402
import saas_mvp.models.line_channel_config as _lcm  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.line_client import (  # noqa: E402
    FakeLinePushClient,
    FakeLineReplyClient,
    get_line_client,
)
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.line_channel_config import LineChannelConfig  # noqa: E402
from saas_mvp.models.reservation import RESERVATION_CANCELLED, Reservation  # noqa: E402
from saas_mvp.models.reservation_reminder import (  # noqa: E402
    REMINDER_PENDING,
    ReservationReminder,
)
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.ops.send_due_reminders import send_due_reminders  # noqa: E402
from saas_mvp.services import booking as booking_svc  # noqa: E402
from saas_mvp.translation import get_translator  # noqa: E402
from saas_mvp.translation.stub import StubTranslator  # noqa: E402

_CHANNEL_SECRET = "confirm_secret_value_0123456789abc"
_ACCESS_TOKEN = "confirm_access_token_value"

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_NOW = datetime.datetime(2030, 6, 1, 9, 0, tzinfo=datetime.timezone.utc)
_SLOT_AT = datetime.datetime(2030, 6, 1, 18, 0, tzinfo=datetime.timezone.utc)

TABLE = "booking_reservations"


# ── migration ────────────────────────────────────────────────────────────────


def _make_old_db(tmp_path):
    url = f"sqlite:///{tmp_path}/old.db"
    eng = create_engine(url, connect_args={"check_same_thread": False})
    with eng.begin() as conn:
        conn.execute(text(
            f"CREATE TABLE {TABLE} ("
            "id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, slot_id INTEGER NOT NULL, "
            "customer_id INTEGER, line_user_id VARCHAR(64), party_size INTEGER NOT NULL DEFAULT 1, "
            "status VARCHAR(16) NOT NULL DEFAULT 'confirmed', note TEXT, "
            "created_at DATETIME, updated_at DATETIME, cancelled_at DATETIME)"
        ))
        conn.execute(text(
            f"INSERT INTO {TABLE} (id, tenant_id, slot_id, party_size) VALUES (1, 1, 1, 2)"
        ))
    return eng


def test_migrate_adds_customer_confirmed_at(tmp_path, monkeypatch):
    eng = _make_old_db(tmp_path)
    monkeypatch.setattr(dbmod, "engine", eng)
    cols = {c["name"] for c in inspect(eng).get_columns(TABLE)}
    assert "customer_confirmed_at" not in cols
    dbmod._migrate_add_reservation_customer_confirmed()
    cols = {c["name"] for c in inspect(eng).get_columns(TABLE)}
    assert "customer_confirmed_at" in cols
    with eng.begin() as conn:
        val = conn.execute(
            text(f"SELECT customer_confirmed_at FROM {TABLE} WHERE id=1")
        ).scalar_one()
    assert val is None


def test_migrate_customer_confirmed_idempotent(tmp_path, monkeypatch):
    eng = _make_old_db(tmp_path)
    monkeypatch.setattr(dbmod, "engine", eng)
    dbmod._migrate_add_reservation_customer_confirmed()
    dbmod._migrate_add_reservation_customer_confirmed()
    assert "customer_confirmed_at" in {
        c["name"] for c in inspect(eng).get_columns(TABLE)
    }


# ── 提醒推播帶互動按鈕 ─────────────────────────────────────────────────────────


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    s = _Session()
    try:
        yield s
    finally:
        s.close()


def _seed_due_reminder(db) -> tuple[int, int]:
    """建 booking 租戶 + 到期提醒，回傳 (tenant_id, reservation_id)。"""
    t = Tenant(name=f"cf_{os.urandom(3).hex()}", plan="free")
    db.add(t)
    db.flush()
    cfg = LineChannelConfig(tenant_id=t.id, default_target_lang="zh-TW")
    cfg.channel_secret = _CHANNEL_SECRET
    cfg.access_token = _ACCESS_TOKEN
    cfg.bot_mode = "booking"
    db.add(cfg)
    slot = BookingSlot(tenant_id=t.id, slot_start=_SLOT_AT, max_capacity=4)
    db.add(slot)
    db.flush()
    resv = booking_svc.book_slot(
        db, tenant_id=t.id, slot_id=slot.id, party_size=2, line_user_id="Ucf"
    )
    # book_slot 已自動入列提醒；將其改為已到期，供 send_due_reminders 撿起。
    reminder = (
        db.query(ReservationReminder)
        .filter(
            ReservationReminder.reservation_id == resv.id,
            ReservationReminder.kind == "day_before",
        )
        .one()
    )
    reminder.status = REMINDER_PENDING
    reminder.remind_at = _NOW - datetime.timedelta(hours=1)
    db.commit()
    return t.id, resv.id


def test_reminder_push_has_confirm_and_cancel_buttons(db):
    _tid, rid = _seed_due_reminder(db)
    fake = FakeLinePushClient()
    results = send_due_reminders(
        session_factory=_Session, push_client=fake, apply=True, now=_NOW
    )
    assert any(r.status == "sent" for r in results)
    sent = fake.sent[0]
    assert sent.quick_reply is not None
    datas = [d for _, d in sent.quick_reply]
    assert f"action=confirm&reservation_id={rid}" in datas
    assert f"action=cancel&reservation_id={rid}" in datas


# ── confirm 服務層 ────────────────────────────────────────────────────────────


class TestConfirmService:
    def test_confirm_sets_timestamp_and_idempotent(self, db):
        tid, rid = _seed_due_reminder(db)
        out = booking_svc.confirm_reservation(
            db, tenant_id=tid, reservation_id=rid, line_user_id="Ucf"
        )
        first = out.customer_confirmed_at
        assert first is not None
        # 重複確認冪等：時間不變
        again = booking_svc.confirm_reservation(
            db, tenant_id=tid, reservation_id=rid, line_user_id="Ucf"
        )
        assert again.customer_confirmed_at == first

    def test_confirm_other_user_rejected(self, db):
        tid, rid = _seed_due_reminder(db)
        with pytest.raises(booking_svc.ReservationPermissionError):
            booking_svc.confirm_reservation(
                db, tenant_id=tid, reservation_id=rid, line_user_id="Uother"
            )

    def test_confirm_cancelled_rejected(self, db):
        tid, rid = _seed_due_reminder(db)
        resv = db.get(Reservation, rid)
        resv.status = RESERVATION_CANCELLED
        db.commit()
        with pytest.raises(booking_svc.ReservationNotFoundError):
            booking_svc.confirm_reservation(
                db, tenant_id=tid, reservation_id=rid, line_user_id="Ucf"
            )


# ── webhook confirm postback 端到端 ──────────────────────────────────────────


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


_EID_SEQ = iter(range(10_000))


def _post_postback(client, tenant_id: int, data: str, *, user="Ucf") -> None:
    event = {
        "type": "postback",
        "replyToken": "rtok",
        "source": {"type": "user", "userId": user},
        "postback": {"data": data},
        "webhookEventId": f"cf-{next(_EID_SEQ)}",
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


class TestWebhookConfirm:
    def test_confirm_postback_sets_timestamp(self, app_client):
        client, line = app_client
        db = _Session()
        try:
            tid, rid = _seed_due_reminder(db)
        finally:
            db.close()

        _post_postback(client, tid, f"action=confirm&reservation_id={rid}")
        assert "已為您確認預約" in line.sent[-1].text

        db = _Session()
        try:
            assert db.get(Reservation, rid).customer_confirmed_at is not None
        finally:
            db.close()

    def test_confirm_other_user_rejected(self, app_client):
        client, line = app_client
        db = _Session()
        try:
            tid, rid = _seed_due_reminder(db)
        finally:
            db.close()

        _post_postback(
            client, tid, f"action=confirm&reservation_id={rid}", user="Uattacker"
        )
        assert "其他人" in line.sent[-1].text
