"""LINE 預約改期對話流程（reschedule → resched_date → resched_slot）。

驗收標準
--------
- 「改期 <編號>」回日期 quick-reply（前向攜帶 reservation_id）
- resched_date 回該日時段 quick-reply
- resched_slot 呼叫原子改期：舊時段回補、新時段扣量、slot_id 更新
- 他人的預約不可改期；已取消不可改期；查無回友善訊息
- 新時段額滿回「已額滿」；同時段 no-op 成功
- 「我的預約」carousel 卡片附「改期」按鈕
- change 通知（BOOKING_NOTIFY 開通時）入列
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
import saas_mvp.models.booking_notification as _bn  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.line_client import FakeLineReplyClient, get_line_client  # noqa: E402
from saas_mvp.models.booking_notification import BookingNotification  # noqa: E402
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.line_channel_config import LineChannelConfig  # noqa: E402
from saas_mvp.models.reservation import (  # noqa: E402
    RESERVATION_CANCELLED,
    RESERVATION_CONFIRMED,
    Reservation,
)
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.services import features as features_svc  # noqa: E402
from saas_mvp.translation import get_translator  # noqa: E402
from saas_mvp.translation.stub import StubTranslator  # noqa: E402

_CHANNEL_SECRET = "resched_secret_value_0123456789abcd"
_ACCESS_TOKEN = "resched_access_token_value"

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_SLOT1 = datetime.datetime(2030, 6, 1, 18, 0, tzinfo=datetime.timezone.utc)
_SLOT2 = datetime.datetime(2030, 6, 2, 19, 0, tzinfo=datetime.timezone.utc)


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


def _seed(*, slot2_capacity: int = 4, notify: bool = False) -> tuple[int, int, int]:
    """建 booking 租戶 + 兩個時段，回傳 (tenant_id, slot1_id, slot2_id)。"""
    db = _Session()
    try:
        t = Tenant(name=f"rs_{os.urandom(3).hex()}", plan="free")
        db.add(t)
        db.flush()
        cfg = LineChannelConfig(tenant_id=t.id, default_target_lang="zh-TW")
        cfg.channel_secret = _CHANNEL_SECRET
        cfg.access_token = _ACCESS_TOKEN
        cfg.bot_mode = "booking"
        db.add(cfg)
        s1 = BookingSlot(tenant_id=t.id, slot_start=_SLOT1, max_capacity=4)
        s2 = BookingSlot(
            tenant_id=t.id, slot_start=_SLOT2, max_capacity=slot2_capacity
        )
        db.add_all([s1, s2])
        db.flush()
        if notify:
            features_svc.set_enabled(
                db, t.id, features_svc.BOOKING_NOTIFY, True,
                actor_user_id=None, source="admin",
            )
        db.commit()
        return t.id, s1.id, s2.id
    finally:
        db.close()


def _text_event(text: str, *, user="Uresched", token="rtok", eid="evt1") -> dict:
    return {
        "type": "message",
        "replyToken": token,
        "source": {"type": "user", "userId": user},
        "message": {"type": "text", "text": text},
        "webhookEventId": eid,
    }


def _postback_event(data: str, *, user="Uresched", token="rtok", eid="evt2") -> dict:
    return {
        "type": "postback",
        "replyToken": token,
        "source": {"type": "user", "userId": user},
        "postback": {"data": data},
        "webhookEventId": eid,
    }


_EID_SEQ = iter(range(10_000))


def _post(client, tenant_id: int, event: dict) -> None:
    event = {**event, "webhookEventId": f"evt-{next(_EID_SEQ)}"}
    body = json.dumps({"destination": "x", "events": [event]}).encode()
    mac = hmac.new(_CHANNEL_SECRET.encode(), body, hashlib.sha256)
    sig = base64.b64encode(mac.digest()).decode()
    r = client.post(
        f"/line/webhook/{tenant_id}",
        content=body,
        headers={"X-Line-Signature": sig, "Content-Type": "application/json"},
    )
    assert r.status_code == 200, r.text


def _book(client, tid: int, slot_id: int, *, user="Uresched", party=2) -> int:
    _post(client, tid, _text_event(f"預約 {slot_id} {party}", user=user))
    db = _Session()
    try:
        resv = db.execute(
            select(Reservation)
            .where(Reservation.tenant_id == tid, Reservation.line_user_id == user)
            .order_by(Reservation.id.desc())
        ).scalars().first()
        assert resv is not None
        return resv.id
    finally:
        db.close()


def _slot(slot_id: int) -> BookingSlot:
    db = _Session()
    try:
        return db.get(BookingSlot, slot_id)
    finally:
        db.close()


def _resv(rid: int) -> Reservation:
    db = _Session()
    try:
        return db.get(Reservation, rid)
    finally:
        db.close()


class TestRescheduleFlow:
    def test_full_three_step_flow(self, app_client):
        client, line = app_client
        tid, s1, s2 = _seed()
        rid = _book(client, tid, s1, party=2)
        assert _slot(s1).booked_count == 2

        # 步驟 1：改期 <編號> → 日期 quick-reply
        line.reset()
        _post(client, tid, _text_event(f"改期 {rid}"))
        assert line.sent, "應回日期選擇"
        qr = line.sent[-1].quick_reply
        assert qr and all("resched_date" in data for _, data in qr)
        assert all(f"reservation_id={rid}" in data for _, data in qr)

        # 步驟 2：選日期 → 時段 quick-reply
        line.reset()
        _post(client, tid, _postback_event(
            f"action=resched_date&reservation_id={rid}&date=2030-06-02"
        ))
        qr = line.sent[-1].quick_reply
        assert qr and all("resched_slot" in data for _, data in qr)

        # 步驟 3：選時段 → 原子改期
        line.reset()
        _post(client, tid, _postback_event(
            f"action=resched_slot&reservation_id={rid}&slot_id={s2}"
        ))
        assert "改期成功" in line.sent[-1].text
        assert _resv(rid).slot_id == s2
        assert _slot(s1).booked_count == 0  # 舊時段回補
        assert _slot(s2).booked_count == 2  # 新時段扣量

    def test_other_users_reservation_rejected(self, app_client):
        client, line = app_client
        tid, s1, s2 = _seed()
        rid = _book(client, tid, s1, user="Uowner")

        line.reset()
        _post(client, tid, _text_event(f"改期 {rid}", user="Uattacker"))
        assert "其他人" in line.sent[-1].text

        line.reset()
        _post(client, tid, _postback_event(
            f"action=resched_slot&reservation_id={rid}&slot_id={s2}",
            user="Uattacker",
        ))
        assert "其他人" in line.sent[-1].text
        assert _resv(rid).slot_id == s1  # 未被改動

    def test_full_slot_rejected(self, app_client):
        client, line = app_client
        tid, s1, s2 = _seed(slot2_capacity=1)
        rid = _book(client, tid, s1, party=2)  # party 2 > slot2 容量 1

        line.reset()
        _post(client, tid, _postback_event(
            f"action=resched_slot&reservation_id={rid}&slot_id={s2}"
        ))
        assert "已額滿" in line.sent[-1].text
        assert _resv(rid).slot_id == s1
        assert _slot(s1).booked_count == 2  # 原時段容量不變

    def test_same_slot_noop_success(self, app_client):
        client, line = app_client
        tid, s1, _s2 = _seed()
        rid = _book(client, tid, s1, party=2)

        line.reset()
        _post(client, tid, _postback_event(
            f"action=resched_slot&reservation_id={rid}&slot_id={s1}"
        ))
        assert "改期成功" in line.sent[-1].text
        assert _slot(s1).booked_count == 2  # 容量未動

    def test_cancelled_reservation_rejected(self, app_client):
        client, line = app_client
        tid, s1, s2 = _seed()
        rid = _book(client, tid, s1)
        db = _Session()
        try:
            resv = db.get(Reservation, rid)
            resv.status = RESERVATION_CANCELLED
            db.commit()
        finally:
            db.close()

        line.reset()
        _post(client, tid, _text_event(f"改期 {rid}"))
        assert "已取消" in line.sent[-1].text

    def test_not_found(self, app_client):
        client, line = app_client
        tid, _s1, _s2 = _seed()
        line.reset()
        _post(client, tid, _text_event("改期 9999"))
        assert "找不到" in line.sent[-1].text

    def test_my_reservations_carousel_has_reschedule_button(self, app_client):
        client, line = app_client
        tid, s1, _s2 = _seed()
        _book(client, tid, s1)
        line.reset()
        _post(client, tid, _text_event("我的預約"))
        assert line.flex, "應回 Flex carousel"
        contents = json.dumps(line.flex[-1].contents, ensure_ascii=False)
        assert "action=reschedule" in contents

    def test_change_notification_enqueued(self, app_client):
        client, line = app_client
        tid, s1, s2 = _seed(notify=True)
        rid = _book(client, tid, s1)
        _post(client, tid, _postback_event(
            f"action=resched_slot&reservation_id={rid}&slot_id={s2}"
        ))
        db = _Session()
        try:
            notifs = list(db.execute(
                select(BookingNotification).where(
                    BookingNotification.tenant_id == tid,
                    BookingNotification.kind == "change",
                )
            ).scalars())
            assert len(notifs) == 1
        finally:
            db.close()

    def test_help_mentions_reschedule(self, app_client):
        client, line = app_client
        tid, _s1, _s2 = _seed()
        _post(client, tid, _text_event("說明"))
        assert "改期" in line.sent[-1].text
