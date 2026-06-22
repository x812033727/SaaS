"""預約異動通知測試 — 取消/改期入列、冪等、文字組裝、派送標記、feature 閘門。"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.models import tenant as _t  # noqa: F401
from saas_mvp.models import customer as _c  # noqa: F401
from saas_mvp.models import booking_slot as _bs  # noqa: F401
from saas_mvp.models import reservation as _r  # noqa: F401
from saas_mvp.models import reservation_reminder as _rr  # noqa: F401
from saas_mvp.models import booking_notification as _bn  # noqa: F401
from saas_mvp.models import tenant_feature as _tf, feature_change_history as _fch  # noqa: F401
import saas_mvp.models.line_channel_config as _lcm  # noqa: F401

from saas_mvp.db import Base
from saas_mvp.line_client import FakeLinePushClient
from saas_mvp.models.booking_notification import (
    NOTIFY_CANCEL,
    NOTIFY_CHANGE,
    NOTIFY_SENT,
    BookingNotification,
)
from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.line_channel_config import LineChannelConfig
from saas_mvp.models.reservation import Reservation
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.tenant_feature import TenantFeature
from saas_mvp.services import booking as booking_svc
from saas_mvp.services import booking_notify as notify_svc
from saas_mvp.ops.send_due_notifications import send_due_notifications

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_UTC = datetime.timezone.utc
_SLOT_START = datetime.datetime(2030, 6, 1, 18, 0, tzinfo=_UTC)
_SLOT2_START = datetime.datetime(2030, 6, 2, 18, 0, tzinfo=_UTC)


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    s = _Session()
    try:
        yield s
    finally:
        s.close()


def _seed_tenant(db, *, bot_mode="booking", notify_enabled=None) -> int:
    t = Tenant(name="notify_test", plan="free")
    db.add(t)
    db.flush()
    cfg = LineChannelConfig(tenant_id=t.id, default_target_lang="zh-TW")
    cfg.channel_secret = "s" * 32
    cfg.access_token = "a" * 40
    cfg.bot_mode = bot_mode
    db.add(cfg)
    if notify_enabled is not None:
        db.add(TenantFeature(
            tenant_id=t.id, feature="BOOKING_NOTIFY", enabled=notify_enabled
        ))
    db.commit()
    return t.id


def _seed_slot(db, tid, start=_SLOT_START) -> int:
    slot = BookingSlot(tenant_id=tid, slot_start=start, max_capacity=10)
    db.add(slot)
    db.commit()
    return slot.id


def _book(db, tid, sid, *, line_user_id="Uabc") -> int:
    resv = booking_svc.book_slot(
        db, tenant_id=tid, slot_id=sid, party_size=2, line_user_id=line_user_id
    )
    return resv.id


def _notifs(db, tid):
    return list(
        db.execute(
            select(BookingNotification).where(BookingNotification.tenant_id == tid)
        ).scalars()
    )


# ── 文字組裝 ─────────────────────────────────────────────────────────────────

class TestTextBuilders:
    def test_change_text(self, db):
        tid = _seed_tenant(db)
        sid = _seed_slot(db, tid)
        rid = _book(db, tid, sid)
        resv = db.get(Reservation, rid)
        slot = db.get(BookingSlot, sid)
        text = notify_svc.build_change_text(resv, slot)
        assert "預約異動" in text and "2030-06-01 18:00" in text and str(rid) in text

    def test_cancel_text(self, db):
        tid = _seed_tenant(db)
        sid = _seed_slot(db, tid)
        rid = _book(db, tid, sid)
        resv = db.get(Reservation, rid)
        slot = db.get(BookingSlot, sid)
        text = notify_svc.build_cancel_text(resv, slot)
        assert "取消" in text and "2030-06-01 18:00" in text


# ── 入列（取消 / 改期） ──────────────────────────────────────────────────────

class TestEnqueue:
    def test_cancel_enqueues(self, db):
        tid = _seed_tenant(db)
        sid = _seed_slot(db, tid)
        rid = _book(db, tid, sid)
        booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=rid)
        notifs = _notifs(db, tid)
        assert len(notifs) == 1 and notifs[0].kind == NOTIFY_CANCEL

    def test_reschedule_enqueues(self, db):
        tid = _seed_tenant(db)
        sid = _seed_slot(db, tid)
        sid2 = _seed_slot(db, tid, start=_SLOT2_START)
        rid = _book(db, tid, sid)
        booking_svc.reschedule_reservation(
            db, tenant_id=tid, reservation_id=rid, new_slot_id=sid2
        )
        notifs = _notifs(db, tid)
        assert len(notifs) == 1 and notifs[0].kind == NOTIFY_CHANGE
        # 容量已搬移
        assert db.get(BookingSlot, sid).booked_count == 0
        assert db.get(BookingSlot, sid2).booked_count == 2
        assert db.get(Reservation, rid).slot_id == sid2

    def test_idempotent_double_cancel(self, db):
        tid = _seed_tenant(db)
        sid = _seed_slot(db, tid)
        rid = _book(db, tid, sid)
        booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=rid)
        booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=rid)
        notifs = [n for n in _notifs(db, tid) if n.kind == NOTIFY_CANCEL]
        assert len(notifs) == 1  # UniqueConstraint 擋第二筆

    def test_no_enqueue_without_line_user(self, db):
        tid = _seed_tenant(db)
        sid = _seed_slot(db, tid)
        # 店家手動建單無 line_user_id
        resv = booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=1)
        booking_svc.cancel_reservation(
            db, tenant_id=tid, reservation_id=resv.id
        )
        assert _notifs(db, tid) == []

    def test_feature_disabled_skips_enqueue(self, db):
        tid = _seed_tenant(db, notify_enabled=False)
        sid = _seed_slot(db, tid)
        rid = _book(db, tid, sid)
        booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=rid)
        assert _notifs(db, tid) == []


# ── 派送（ops 腳本，注入 fake push + session） ───────────────────────────────

class TestSendDue:
    def test_apply_marks_sent(self, db):
        tid = _seed_tenant(db)
        sid = _seed_slot(db, tid)
        rid = _book(db, tid, sid)
        booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=rid)
        now = datetime.datetime.now(_UTC)
        fake = FakeLinePushClient()
        results = send_due_notifications(
            session_factory=_Session, push_client=fake, apply=True, now=now
        )
        assert [r.status for r in results] == ["sent"]
        assert fake.call_count == 1 and "取消" in fake.texts[0]
        notifs = _notifs(db, tid)
        assert notifs[0].status == NOTIFY_SENT

    def test_idempotent_resend(self, db):
        tid = _seed_tenant(db)
        sid = _seed_slot(db, tid)
        rid = _book(db, tid, sid)
        booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=rid)
        now = datetime.datetime.now(_UTC)
        fake = FakeLinePushClient()
        send_due_notifications(
            session_factory=_Session, push_client=fake, apply=True, now=now
        )
        second = send_due_notifications(
            session_factory=_Session, push_client=fake, apply=True, now=now
        )
        assert second == []  # 已 sent，無 due
        assert fake.call_count == 1

    def test_dry_run_sends_nothing(self, db):
        tid = _seed_tenant(db)
        sid = _seed_slot(db, tid)
        rid = _book(db, tid, sid)
        booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=rid)
        now = datetime.datetime.now(_UTC)
        fake = FakeLinePushClient()
        results = send_due_notifications(
            session_factory=_Session, push_client=fake, apply=False, now=now
        )
        assert [r.status for r in results] == ["would_send"]
        assert fake.call_count == 0

    def test_feature_disabled_skips_send(self, db):
        tid = _seed_tenant(db)
        sid = _seed_slot(db, tid)
        rid = _book(db, tid, sid)
        booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=rid)
        # 入列後租戶退訂 BOOKING_NOTIFY → 派送應跳過。
        db.add(TenantFeature(
            tenant_id=tid, feature="BOOKING_NOTIFY", enabled=False
        ))
        db.commit()
        now = datetime.datetime.now(_UTC)
        fake = FakeLinePushClient()
        results = send_due_notifications(
            session_factory=_Session, push_client=fake, apply=True, now=now
        )
        assert [r.status for r in results] == ["skipped"]
        assert fake.call_count == 0
