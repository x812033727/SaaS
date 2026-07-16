"""額滿候補（waitlist）— 登記 / 冪等 / 回補通知 / 額度 / 隔離。

驗收標準
--------
- 額滿才可登記；尚有名額拋 SlotNotFullError（引導直接預約）
- 重複登記 reactivate 既有列（冪等,一 slot 一 user 一列）
- 取消預約回補 → 第一位人數符合的候補標 notified + 推播（含「立即預約」
  quick reply）；party 不符者跳過取下一位
- 改期回補舊時段亦觸發通知
- 推播額度罄 / 推播失敗 → 候補退回 waiting,取消主流程不受影響
- 租戶隔離：他租戶的候補不會被通知
- LINE webhook 端到端：額滿回覆附「加入候補」按鈕、候補查詢/取消
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
import saas_mvp.models.booking_waitlist as _wl  # noqa: F401,E402
import saas_mvp.models.push_usage as _pu  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.config import settings  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.line_client import (  # noqa: E402
    FakeLinePushClient,
    FakeLineReplyClient,
    get_line_client,
)
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.booking_waitlist import (  # noqa: E402
    WAITLIST_BOOKED,
    WAITLIST_CANCELLED,
    WAITLIST_EXPIRED,
    WAITLIST_NOTIFIED,
    WAITLIST_WAITING,
    WaitlistEntry,
)
from saas_mvp.models.line_channel_config import LineChannelConfig  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.services import booking as booking_svc  # noqa: E402
from saas_mvp.services import slots as slots_svc  # noqa: E402
from saas_mvp.services import waitlist as waitlist_svc  # noqa: E402
from saas_mvp.translation import get_translator  # noqa: E402
from saas_mvp.translation.stub import StubTranslator  # noqa: E402

_CHANNEL_SECRET = "waitlist_secret_value_0123456789ab"
_ACCESS_TOKEN = "waitlist_access_token_value"

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_SLOT_AT = datetime.datetime(2030, 6, 1, 18, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    s = _Session()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def fake_push(monkeypatch):
    fake = FakeLinePushClient()
    monkeypatch.setattr(waitlist_svc, "_default_push_client", lambda: fake)
    return fake


def _tenant(db, *, with_line: bool = True) -> int:
    t = Tenant(name=f"wl_{os.urandom(3).hex()}", plan="free")
    db.add(t)
    db.flush()
    if with_line:
        cfg = LineChannelConfig(tenant_id=t.id, default_target_lang="zh-TW")
        cfg.channel_secret = _CHANNEL_SECRET
        cfg.access_token = _ACCESS_TOKEN
        cfg.bot_mode = "booking"
        db.add(cfg)
    db.commit()
    return t.id


def _slot(db, tid: int, *, cap: int = 2) -> int:
    s = BookingSlot(tenant_id=tid, slot_start=_SLOT_AT, max_capacity=cap)
    db.add(s)
    db.commit()
    return s.id


def _fill_slot(db, tid: int, slot_id: int, *, party: int = 2, user="Ufill") -> int:
    resv = booking_svc.book_slot(
        db, tenant_id=tid, slot_id=slot_id, party_size=party, line_user_id=user
    )
    return resv.id


def _entry(db, entry_id: int) -> WaitlistEntry:
    db.expire_all()
    return db.get(WaitlistEntry, entry_id)


class TestJoin:
    def test_join_requires_full_slot(self, db):
        tid = _tenant(db)
        sid = _slot(db, tid, cap=2)
        with pytest.raises(waitlist_svc.SlotNotFullError):
            waitlist_svc.join_waitlist(
                db, tenant_id=tid, slot_id=sid, line_user_id="Uw", party_size=1
            )

    def test_join_when_full_and_idempotent_reactivate(self, db):
        tid = _tenant(db)
        sid = _slot(db, tid, cap=2)
        _fill_slot(db, tid, sid)

        e1 = waitlist_svc.join_waitlist(
            db, tenant_id=tid, slot_id=sid, line_user_id="Uw", party_size=1
        )
        assert e1.status == WAITLIST_WAITING

        # 重複登記：同一列 reactivate,更新人數
        e2 = waitlist_svc.join_waitlist(
            db, tenant_id=tid, slot_id=sid, line_user_id="Uw", party_size=2
        )
        assert e2.id == e1.id
        assert e2.party_size == 2
        rows = list(db.execute(select(WaitlistEntry)).scalars())
        assert len(rows) == 1

    def test_join_unknown_slot(self, db):
        tid = _tenant(db)
        with pytest.raises(waitlist_svc.WaitlistSlotNotFound):
            waitlist_svc.join_waitlist(
                db, tenant_id=tid, slot_id=999, line_user_id="Uw"
            )


class TestCancelReleaseNotify:
    def test_cancel_notifies_first_eligible(self, db, fake_push):
        tid = _tenant(db)
        sid = _slot(db, tid, cap=2)
        rid = _fill_slot(db, tid, sid, party=2)
        e = waitlist_svc.join_waitlist(
            db, tenant_id=tid, slot_id=sid, line_user_id="Uwait", party_size=1
        )

        booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=rid)

        fresh = _entry(db, e.id)
        assert fresh.status == WAITLIST_NOTIFIED
        assert fresh.notified_at is not None
        assert fake_push.call_count == 1
        sent = fake_push.sent[0]
        assert sent.to_user_id == "Uwait"
        assert sent.quick_reply is not None
        label, data = sent.quick_reply[0]
        assert label == "立即預約"
        assert f"slot_id={sid}" in data and "party=1" in data

    def test_party_too_big_skipped_next_taken(self, db, fake_push):
        tid = _tenant(db)
        sid = _slot(db, tid, cap=3)
        rid = _fill_slot(db, tid, sid, party=1, user="Ufill1")
        _fill_slot(db, tid, sid, party=2, user="Ufill2")
        # 候補1 要 3 位（回補後僅 1 可用,不符）、候補2 要 1 位（符合）
        big = waitlist_svc.join_waitlist(
            db, tenant_id=tid, slot_id=sid, line_user_id="Ubig", party_size=3
        )
        small = waitlist_svc.join_waitlist(
            db, tenant_id=tid, slot_id=sid, line_user_id="Usmall", party_size=1
        )

        booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=rid)

        assert _entry(db, big.id).status == WAITLIST_WAITING  # 跳過
        assert _entry(db, small.id).status == WAITLIST_NOTIFIED
        assert fake_push.call_count == 1
        assert fake_push.sent[0].to_user_id == "Usmall"

    def test_reschedule_releases_old_slot(self, db, fake_push):
        tid = _tenant(db)
        s1 = _slot(db, tid, cap=2)
        db.add(BookingSlot(
            tenant_id=tid,
            slot_start=_SLOT_AT + datetime.timedelta(days=1),
            max_capacity=4,
        ))
        db.commit()
        s2 = db.execute(
            select(BookingSlot.id).where(BookingSlot.tenant_id == tid)
            .order_by(BookingSlot.id.desc())
        ).scalars().first()
        rid = _fill_slot(db, tid, s1, party=2)
        e = waitlist_svc.join_waitlist(
            db, tenant_id=tid, slot_id=s1, line_user_id="Uwait", party_size=2
        )

        booking_svc.reschedule_reservation(
            db, tenant_id=tid, reservation_id=rid, new_slot_id=s2
        )

        assert _entry(db, e.id).status == WAITLIST_NOTIFIED
        assert fake_push.call_count == 1

    def test_quota_exhausted_reverts_to_waiting(self, db, fake_push, monkeypatch):
        monkeypatch.setattr(settings, "push_allowance_base", 0)
        tid = _tenant(db)
        sid = _slot(db, tid, cap=2)
        rid = _fill_slot(db, tid, sid, party=2)
        e = waitlist_svc.join_waitlist(
            db, tenant_id=tid, slot_id=sid, line_user_id="Uwait", party_size=1
        )

        resv = booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=rid)

        assert resv.status == "cancelled"  # 主流程不受影響
        assert _entry(db, e.id).status == WAITLIST_WAITING  # 退回等候
        assert fake_push.call_count == 0

    def test_push_failure_reverts_and_cancel_succeeds(self, db, monkeypatch):
        failing = FakeLinePushClient(fail=True)
        monkeypatch.setattr(waitlist_svc, "_default_push_client", lambda: failing)
        tid = _tenant(db)
        sid = _slot(db, tid, cap=2)
        rid = _fill_slot(db, tid, sid, party=2)
        e = waitlist_svc.join_waitlist(
            db, tenant_id=tid, slot_id=sid, line_user_id="Uwait", party_size=1
        )

        resv = booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=rid)

        assert resv.status == "cancelled"
        assert _entry(db, e.id).status == WAITLIST_WAITING

    def test_no_line_config_reverts(self, db, fake_push):
        tid = _tenant(db, with_line=False)
        sid = _slot(db, tid, cap=2)
        rid = _fill_slot(db, tid, sid, party=2)
        e = waitlist_svc.join_waitlist(
            db, tenant_id=tid, slot_id=sid, line_user_id="Uwait", party_size=1
        )
        booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=rid)
        assert _entry(db, e.id).status == WAITLIST_WAITING
        assert fake_push.call_count == 0

    def test_tenant_isolation(self, db, fake_push):
        tid_a = _tenant(db)
        tid_b = _tenant(db)
        sid_a = _slot(db, tid_a, cap=2)
        sid_b = _slot(db, tid_b, cap=2)
        rid_a = _fill_slot(db, tid_a, sid_a, party=2)
        _fill_slot(db, tid_b, sid_b, party=2, user="Ub")
        e_b = waitlist_svc.join_waitlist(
            db, tenant_id=tid_b, slot_id=sid_b, line_user_id="Uwait_b",
            party_size=1,
        )

        booking_svc.cancel_reservation(db, tenant_id=tid_a, reservation_id=rid_a)

        # A 租戶回補不會通知 B 租戶的候補
        assert _entry(db, e_b.id).status == WAITLIST_WAITING
        assert fake_push.call_count == 0

    def test_offer_expires_and_scheduler_moves_to_next(self, db, fake_push):
        tid = _tenant(db)
        tenant = db.get(Tenant, tid)
        tenant.waitlist_offer_minutes = 10
        db.commit()
        sid = _slot(db, tid, cap=2)
        rid = _fill_slot(db, tid, sid, party=2)
        first = waitlist_svc.join_waitlist(
            db, tenant_id=tid, slot_id=sid, line_user_id="Ufirst", party_size=1
        )
        second = waitlist_svc.join_waitlist(
            db, tenant_id=tid, slot_id=sid, line_user_id="Usecond", party_size=1
        )

        booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=rid)
        offered = _entry(db, first.id)
        assert offered.status == WAITLIST_NOTIFIED
        assert offered.offer_expires_at is not None
        assert offered.notification_attempts == 1

        # 有效期限內不會同時通知第二人。
        assert waitlist_svc.notify_next_for_slot_best_effort(
            db,
            tenant_id=tid,
            slot_id=sid,
            push_client=fake_push,
            now=offered.notified_at + datetime.timedelta(minutes=5),
        ) is False
        assert _entry(db, second.id).status == WAITLIST_WAITING

        # 逾時後第一人結束，第二人立即收到 offer。
        assert waitlist_svc.notify_next_for_slot_best_effort(
            db,
            tenant_id=tid,
            slot_id=sid,
            push_client=fake_push,
            now=offered.notified_at + datetime.timedelta(minutes=11),
        ) is True
        assert _entry(db, first.id).status == WAITLIST_EXPIRED
        assert _entry(db, second.id).status == WAITLIST_NOTIFIED
        assert fake_push.sent[-1].to_user_id == "Usecond"

    def test_successful_booking_fulfills_waitlist_and_keeps_choices(
        self, db, fake_push
    ):
        from saas_mvp.models.service import Service
        from saas_mvp.models.staff import Staff

        tid = _tenant(db)
        service = Service(tenant_id=tid, name="染髮", duration_minutes=60)
        staff = Staff(tenant_id=tid, name="Amy")
        db.add_all([service, staff])
        db.flush()
        sid = _slot(db, tid, cap=1)
        rid = _fill_slot(db, tid, sid, party=1)
        entry = waitlist_svc.join_waitlist(
            db,
            tenant_id=tid,
            slot_id=sid,
            line_user_id="Ucandidate",
            party_size=1,
            service_id=service.id,
            staff_id=staff.id,
        )
        booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=rid)

        reservation = booking_svc.book_slot(
            db,
            tenant_id=tid,
            slot_id=sid,
            party_size=1,
            line_user_id="Ucandidate",
            service_id=service.id,
            staff_id=staff.id,
        )
        fulfilled = _entry(db, entry.id)
        assert fulfilled.status == WAITLIST_BOOKED
        assert fulfilled.reservation_id == reservation.id
        assert reservation.service_id == service.id
        assert reservation.staff_id == staff.id

    def test_capacity_increase_immediately_notifies(self, db, fake_push):
        from saas_mvp.services import slots as slots_svc

        tid = _tenant(db)
        sid = _slot(db, tid, cap=1)
        _fill_slot(db, tid, sid, party=1)
        entry = waitlist_svc.join_waitlist(
            db, tenant_id=tid, slot_id=sid, line_user_id="Ucapacity", party_size=1
        )

        slots_svc.update_slot(db, tenant_id=tid, slot_id=sid, max_capacity=2)

        assert _entry(db, entry.id).status == WAITLIST_NOTIFIED
        assert fake_push.sent[-1].to_user_id == "Ucapacity"


class TestCancelWaitlist:
    def test_cancel_own_entry(self, db):
        tid = _tenant(db)
        sid = _slot(db, tid, cap=2)
        _fill_slot(db, tid, sid)
        e = waitlist_svc.join_waitlist(
            db, tenant_id=tid, slot_id=sid, line_user_id="Uw", party_size=1
        )
        out = waitlist_svc.cancel_waitlist(
            db, tenant_id=tid, entry_id=e.id, line_user_id="Uw"
        )
        assert out.status == WAITLIST_CANCELLED

    def test_cancel_others_entry_rejected(self, db):
        tid = _tenant(db)
        sid = _slot(db, tid, cap=2)
        _fill_slot(db, tid, sid)
        e = waitlist_svc.join_waitlist(
            db, tenant_id=tid, slot_id=sid, line_user_id="Uw", party_size=1
        )
        with pytest.raises(waitlist_svc.WaitlistEntryNotFound):
            waitlist_svc.cancel_waitlist(
                db, tenant_id=tid, entry_id=e.id, line_user_id="Uother"
            )

    def test_cancelled_entry_not_picked(self, db, fake_push):
        tid = _tenant(db)
        sid = _slot(db, tid, cap=2)
        rid = _fill_slot(db, tid, sid, party=2)
        e = waitlist_svc.join_waitlist(
            db, tenant_id=tid, slot_id=sid, line_user_id="Uw", party_size=1
        )
        waitlist_svc.cancel_waitlist(
            db, tenant_id=tid, entry_id=e.id, line_user_id="Uw"
        )
        booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=rid)
        assert _entry(db, e.id).status == WAITLIST_CANCELLED
        assert fake_push.call_count == 0

    def test_cancel_notified_entry_immediately_offers_next(self, db, fake_push):
        tid = _tenant(db)
        sid = _slot(db, tid, cap=1)
        rid = _fill_slot(db, tid, sid, party=1)
        first = waitlist_svc.join_waitlist(
            db, tenant_id=tid, slot_id=sid, line_user_id="Ufirst", party_size=1
        )
        second = waitlist_svc.join_waitlist(
            db, tenant_id=tid, slot_id=sid, line_user_id="Usecond", party_size=1
        )
        booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=rid)

        waitlist_svc.cancel_waitlist(
            db, tenant_id=tid, entry_id=first.id, line_user_id="Ufirst"
        )

        assert _entry(db, first.id).status == WAITLIST_CANCELLED
        assert _entry(db, second.id).status == WAITLIST_NOTIFIED
        assert fake_push.sent[-1].to_user_id == "Usecond"


# ── LINE webhook 端到端 ─────────────────────────────────────────────────────


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


def _post(client, tenant_id: int, event: dict) -> None:
    event = {**event, "webhookEventId": f"wl-{next(_EID_SEQ)}"}
    body = json.dumps({"destination": "x", "events": [event]}).encode()
    mac = hmac.new(_CHANNEL_SECRET.encode(), body, hashlib.sha256)
    sig = base64.b64encode(mac.digest()).decode()
    r = client.post(
        f"/line/webhook/{tenant_id}",
        content=body,
        headers={"X-Line-Signature": sig, "Content-Type": "application/json"},
    )
    assert r.status_code == 200, r.text


def _text_event(text: str, *, user="Uwl") -> dict:
    return {
        "type": "message",
        "replyToken": "rtok",
        "source": {"type": "user", "userId": user},
        "message": {"type": "text", "text": text},
    }


def _postback_event(data: str, *, user="Uwl") -> dict:
    return {
        "type": "postback",
        "replyToken": "rtok",
        "source": {"type": "user", "userId": user},
        "postback": {"data": data},
    }


class TestWebhookFlow:
    def test_full_slot_reply_offers_waitlist_button(self, app_client):
        client, line = app_client
        db = _Session()
        try:
            tid = _tenant(db)
            sid = _slot(db, tid, cap=1)
            _fill_slot(db, tid, sid, party=1)
        finally:
            db.close()

        _post(client, tid, _text_event(f"預約 {sid} 1"))
        reply = line.sent[-1]
        assert "已額滿" in reply.text
        assert reply.quick_reply
        assert any("waitlist_join" in d for _, d in reply.quick_reply)

    def test_join_view_and_cancel_via_postback(self, app_client):
        client, line = app_client
        db = _Session()
        try:
            tid = _tenant(db)
            sid = _slot(db, tid, cap=1)
            _fill_slot(db, tid, sid, party=1)
        finally:
            db.close()

        _post(client, tid, _postback_event(
            f"action=waitlist_join&slot_id={sid}&party=1"
        ))
        assert "已加入候補" in line.sent[-1].text

        _post(client, tid, _text_event("候補"))
        reply = line.sent[-1]
        assert "你的候補" in reply.text
        assert reply.quick_reply
        entry_data = reply.quick_reply[0][1]
        assert "waitlist_cancel" in entry_data

        _post(client, tid, _postback_event(entry_data))
        assert "候補已取消" in line.sent[-1].text

    def test_join_not_full_slot_redirects_to_booking(self, app_client):
        client, line = app_client
        db = _Session()
        try:
            tid = _tenant(db)
            sid = _slot(db, tid, cap=4)
        finally:
            db.close()

        _post(client, tid, _postback_event(
            f"action=waitlist_join&slot_id={sid}&party=1"
        ))
        assert "有名額" in line.sent[-1].text


class TestCapacityHold:
    """R4-B1:發 offer 為候補者保留容量,非 offeree 訂不到、offeree 訂得到。"""

    def _slot_obj(self, db, sid):
        db.expire_all()
        return db.get(BookingSlot, sid)

    def test_offer_holds_capacity_and_blocks_others(self, db, fake_push):
        tid = _tenant(db)
        sid = _slot(db, tid, cap=2)
        rid = _fill_slot(db, tid, sid, party=2)  # 額滿
        wl = waitlist_svc.join_waitlist(
            db, tenant_id=tid, slot_id=sid, line_user_id="Uwait", party_size=1
        )
        booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=rid)  # 釋 2
        assert _entry(db, wl.id).status == WAITLIST_NOTIFIED
        slot = self._slot_obj(db, sid)
        assert slot.held_count == 1  # 為候補保留 1
        # 陌生人只能訂到未保留的 1 個(2 釋出 - 1 保留),要 2 位會被擋
        assert slot.online_available == 1
        with pytest.raises(booking_svc.SlotFullError):
            booking_svc.book_slot(
                db, tenant_id=tid, slot_id=sid, party_size=2, line_user_id="Ustranger"
            )

    def test_offeree_can_consume_own_hold(self, db, fake_push):
        tid = _tenant(db)
        sid = _slot(db, tid, cap=1)
        rid = _fill_slot(db, tid, sid, party=1)  # 額滿(cap 1)
        wl = waitlist_svc.join_waitlist(
            db, tenant_id=tid, slot_id=sid, line_user_id="Uwait", party_size=1
        )
        booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=rid)  # 釋 1
        assert _entry(db, wl.id).status == WAITLIST_NOTIFIED
        slot = self._slot_obj(db, sid)
        assert slot.held_count == 1 and slot.online_available == 0  # 公開池 0
        # 陌生人訂不到(名額被保留)
        with pytest.raises(booking_svc.SlotFullError):
            booking_svc.book_slot(
                db, tenant_id=tid, slot_id=sid, party_size=1, line_user_id="Ustranger"
            )
        # 但 offeree 本人訂得到(own_hold 加回)
        resv = booking_svc.book_slot(
            db, tenant_id=tid, slot_id=sid, party_size=1, line_user_id="Uwait"
        )
        assert resv is not None
        slot = self._slot_obj(db, sid)
        assert slot.held_count == 0  # 建單消耗 hold
        assert _entry(db, wl.id).status == WAITLIST_BOOKED

    def test_offer_expiry_releases_hold(self, db, fake_push):
        tid = _tenant(db)
        tenant = db.get(Tenant, tid)
        tenant.waitlist_offer_minutes = 10
        db.commit()
        sid = _slot(db, tid, cap=1)
        rid = _fill_slot(db, tid, sid, party=1)
        wl = waitlist_svc.join_waitlist(
            db, tenant_id=tid, slot_id=sid, line_user_id="Uwait", party_size=1
        )
        booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=rid)
        offered = _entry(db, wl.id)
        assert self._slot_obj(db, sid).held_count == 1
        # 逾時後回補掃描:釋放 hold(此時無下一位候補)
        waitlist_svc.notify_next_for_slot_best_effort(
            db, tenant_id=tid, slot_id=sid, push_client=fake_push,
            now=offered.notified_at + datetime.timedelta(minutes=11),
        )
        slot = self._slot_obj(db, sid)
        assert slot.held_count == 0 and slot.online_available == 1  # 名額回公開池
        assert _entry(db, wl.id).status == WAITLIST_EXPIRED

    def test_cancel_waitlist_releases_hold(self, db, fake_push):
        tid = _tenant(db)
        sid = _slot(db, tid, cap=1)
        rid = _fill_slot(db, tid, sid, party=1)
        wl = waitlist_svc.join_waitlist(
            db, tenant_id=tid, slot_id=sid, line_user_id="Uwait", party_size=1
        )
        booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=rid)
        assert self._slot_obj(db, sid).held_count == 1
        waitlist_svc.cancel_waitlist(
            db, tenant_id=tid, entry_id=wl.id, line_user_id="Uwait"
        )
        assert self._slot_obj(db, sid).held_count == 0  # 取消候補釋放保留

    def test_shrink_capacity_below_held_blocked(self, db, fake_push):
        tid = _tenant(db)
        sid = _slot(db, tid, cap=2)
        rid = _fill_slot(db, tid, sid, party=2)
        waitlist_svc.join_waitlist(
            db, tenant_id=tid, slot_id=sid, line_user_id="Uwait", party_size=1
        )
        booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=rid)
        # held=1;縮容量到 0 會偷走保留名額 → 409
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            slots_svc.update_slot(
                db, tenant_id=tid, slot_id=sid, max_capacity=0,
            )
        assert exc.value.status_code == 409


def test_check_waitlist_holds_invariant(db, fake_push):
    """R4-B1 不變量檢核腳本:正常無飄移;人為戳壞可抓出。"""
    from saas_mvp.ops.check_waitlist_holds import find_drift

    tid = _tenant(db)
    sid = _slot(db, tid, cap=2)
    rid = _fill_slot(db, tid, sid, party=2)
    waitlist_svc.join_waitlist(
        db, tenant_id=tid, slot_id=sid, line_user_id="Uwait", party_size=1
    )
    booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=rid)
    factory = sessionmaker(bind=db.get_bind())
    assert find_drift(session_factory=factory) == []  # held_count == sum(hold)
    # 人為戳壞 held_count → 被抓出
    slot = db.get(BookingSlot, sid)
    slot.held_count = 99
    db.commit()
    drift = find_drift(session_factory=factory)
    assert len(drift) == 1 and drift[0]["slot_id"] == sid
