"""預約提醒測試 — 入列、到期派送（冪等）、取消跳過、dry-run、非 booking 跳過。"""

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
import saas_mvp.models.line_channel_config as _lcm  # noqa: F401

from saas_mvp.db import Base
from saas_mvp.line_client import FakeLinePushClient
from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.line_channel_config import LineChannelConfig
from saas_mvp.models.reservation_reminder import (
    REMINDER_DAY_BEFORE,
    REMINDER_DAY_OF,
    ReservationReminder,
)
from saas_mvp.models.tenant import Tenant
from saas_mvp.services import booking as booking_svc
from saas_mvp.ops.send_due_reminders import send_due_reminders

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_SLOT_START = datetime.datetime(2030, 6, 1, 18, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    s = _Session()
    try:
        yield s
    finally:
        s.close()


def _seed(db, *, bot_mode="booking") -> int:
    t = Tenant(name="rem_test", plan="free")
    db.add(t)
    db.flush()
    cfg = LineChannelConfig(tenant_id=t.id, default_target_lang="zh-TW")
    cfg.channel_secret = "s" * 32
    cfg.access_token = "a" * 40
    cfg.bot_mode = bot_mode
    db.add(cfg)
    db.commit()
    return t.id


def _seed_slot(db, tid) -> int:
    slot = BookingSlot(tenant_id=tid, slot_start=_SLOT_START, max_capacity=10)
    db.add(slot)
    db.commit()
    return slot.id


def _reminders(db, tid):
    return list(
        db.execute(
            select(ReservationReminder).where(ReservationReminder.tenant_id == tid)
        ).scalars()
    )


class TestEnqueue:
    def test_booking_enqueues_two_reminders(self, db):
        tid = _seed(db)
        sid = _seed_slot(db, tid)
        booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=2, line_user_id="Urem")
        rems = _reminders(db, tid)
        kinds = {r.kind for r in rems}
        assert kinds == {REMINDER_DAY_BEFORE, REMINDER_DAY_OF}
        expected_day_before = _SLOT_START - datetime.timedelta(days=1)
        for r in rems:
            assert r.status == "pending"
            if r.kind == REMINDER_DAY_BEFORE:
                # SQLite 不保存時區，讀回為 naive；比較時統一去除 tzinfo。
                got = r.remind_at.replace(tzinfo=None)
                assert got == expected_day_before.replace(tzinfo=None)

    def test_no_reminder_without_line_user(self, db):
        """店家端建單（無 line_user_id）不入列提醒。"""
        tid = _seed(db)
        sid = _seed_slot(db, tid)
        booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=2)
        assert _reminders(db, tid) == []


class TestSend:
    def test_due_sent_once_then_idempotent(self, db):
        tid = _seed(db)
        sid = _seed_slot(db, tid)
        booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=1, line_user_id="Urem")
        now = _SLOT_START + datetime.timedelta(minutes=1)  # 兩筆皆到期
        fake = FakeLinePushClient()

        first = send_due_reminders(
            session_factory=_Session, push_client=fake, apply=True, now=now
        )
        sent = [r for r in first if r.status == "sent"]
        assert len(sent) == 2
        assert fake.call_count == 2

        # 重跑：不重送（冪等）
        second = send_due_reminders(
            session_factory=_Session, push_client=fake, apply=True, now=now
        )
        assert all(r.status != "sent" for r in second)
        assert fake.call_count == 2  # 沒有新增推播

    def test_not_due_not_sent(self, db):
        tid = _seed(db)
        sid = _seed_slot(db, tid)
        booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=1, line_user_id="Urem")
        early = _SLOT_START - datetime.timedelta(days=10)  # 尚未到任何提醒時點
        fake = FakeLinePushClient()
        results = send_due_reminders(
            session_factory=_Session, push_client=fake, apply=True, now=early
        )
        assert results == []
        assert fake.call_count == 0

    def test_dry_run_sends_nothing(self, db):
        tid = _seed(db)
        sid = _seed_slot(db, tid)
        booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=1, line_user_id="Urem")
        now = _SLOT_START + datetime.timedelta(minutes=1)
        fake = FakeLinePushClient()
        results = send_due_reminders(
            session_factory=_Session, push_client=fake, apply=False, now=now
        )
        assert fake.call_count == 0
        assert all(r.status == "would_send" for r in results)
        # 仍為 pending
        assert all(r.status == "pending" for r in _reminders(db, tid))

    def test_cancelled_reservation_skipped(self, db):
        tid = _seed(db)
        sid = _seed_slot(db, tid)
        resv = booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=1, line_user_id="Urem")
        booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=resv.id)
        now = _SLOT_START + datetime.timedelta(minutes=1)
        fake = FakeLinePushClient()
        # 取消已把 pending → skipped，故沒有 due pending
        results = send_due_reminders(
            session_factory=_Session, push_client=fake, apply=True, now=now
        )
        assert fake.call_count == 0
        assert results == []

    def test_non_booking_mode_skipped(self, db):
        """租戶非 booking 模式 → 提醒跳過、不推播。"""
        tid = _seed(db, bot_mode="booking")
        sid = _seed_slot(db, tid)
        booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=1, line_user_id="Urem")
        # 事後改為 translation
        cfg = db.execute(
            select(LineChannelConfig).where(LineChannelConfig.tenant_id == tid)
        ).scalar_one()
        cfg.bot_mode = "translation"
        db.commit()
        now = _SLOT_START + datetime.timedelta(minutes=1)
        fake = FakeLinePushClient()
        results = send_due_reminders(
            session_factory=_Session, push_client=fake, apply=True, now=now
        )
        assert fake.call_count == 0
        assert all(r.status == "skipped" for r in results)

    def test_push_failure_marked_failed(self, db):
        tid = _seed(db)
        sid = _seed_slot(db, tid)
        booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=1, line_user_id="Urem")
        now = _SLOT_START + datetime.timedelta(minutes=1)
        fake = FakeLinePushClient(fail=True)
        results = send_due_reminders(
            session_factory=_Session, push_client=fake, apply=True, now=now
        )
        assert all(r.status == "failed" for r in results)
        assert all(r.status == "failed" for r in _reminders(db, tid))
