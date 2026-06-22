"""月度推播額度測試（vibeaico「Additional Push Notification Allowance」）。

涵蓋：
  * 月份 period keying（'YYYYMM'）；跨月歸零。
  * allowance：base；開通 PUSH_BOOST → base + boost。
  * has_push_quota 邊界；consume 遞增 + FOR-UPDATE upsert。
  * 提醒 / 異動通知 / 行銷 send loop 在額度用罄後停止送出（其餘標 skipped、不送）。
  * GET /quota/push 回傳正確數字。
  * 租戶隔離：一租戶用量不計入另一租戶。
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# 模型註冊（讓各 model 進入 SQLAlchemy registry，本檔可單獨執行）。
from saas_mvp.models import tenant as _t  # noqa: F401
from saas_mvp.models import user as _u  # noqa: F401
from saas_mvp.models import customer as _c  # noqa: F401
from saas_mvp.models import booking_slot as _bs  # noqa: F401
from saas_mvp.models import reservation as _r  # noqa: F401
from saas_mvp.models import reservation_reminder as _rr  # noqa: F401
from saas_mvp.models import booking_notification as _bn  # noqa: F401
from saas_mvp.models import campaign as _camp  # noqa: F401
from saas_mvp.models import campaign_send as _cs  # noqa: F401
from saas_mvp.models import tenant_feature as _tf  # noqa: F401
from saas_mvp.models import feature_change_history as _fch  # noqa: F401
from saas_mvp.models import push_usage as _pu  # noqa: F401
import saas_mvp.models.line_channel_config as _lcm  # noqa: F401

from saas_mvp.app import create_app
from saas_mvp.config import settings
from saas_mvp.db import Base, get_db
from saas_mvp.line_client import FakeLinePushClient
from saas_mvp.models.booking_notification import (
    NOTIFY_PENDING,
    NOTIFY_SENT,
    NOTIFY_SKIPPED,
    BookingNotification,
)
from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.campaign import CAMPAIGN_BROADCAST, Campaign
from saas_mvp.models.campaign_send import (
    CAMPAIGN_SEND_SENT,
    CAMPAIGN_SEND_SKIPPED,
    CampaignSend,
)
from saas_mvp.models.customer import Customer
from saas_mvp.models.line_channel_config import LineChannelConfig
from saas_mvp.models.push_usage import PushUsage
from saas_mvp.models.reservation_reminder import (
    REMINDER_DAY_BEFORE,
    REMINDER_PENDING,
    REMINDER_SENT,
    REMINDER_SKIPPED,
    ReservationReminder,
)
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.user import User
from saas_mvp.ops.send_due_notifications import send_due_notifications
from saas_mvp.ops.send_due_reminders import send_due_reminders
from saas_mvp.services import features as features_svc
from saas_mvp.services import marketing as marketing_svc
from saas_mvp.services import push_quota as push_quota_svc

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_NOW = datetime.datetime(2030, 6, 15, 9, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    s = _Session()
    try:
        yield s
    finally:
        s.close()


def _tenant(db) -> int:
    t = Tenant(name=f"t_{uuid.uuid4().hex[:6]}", plan="free")
    db.add(t)
    db.flush()
    db.commit()
    return t.id


# ── period keying ─────────────────────────────────────────────────────────────

def test_period_now_is_yyyymm():
    assert push_quota_svc._period_now(_NOW) == "203006"
    other = datetime.datetime(2031, 1, 5, tzinfo=datetime.timezone.utc)
    assert push_quota_svc._period_now(other) == "203101"


def test_usage_keyed_by_month(db):
    tid = _tenant(db)
    push_quota_svc.consume_push(db, tid, now=_NOW, n=3)
    assert push_quota_svc.get_usage(db, tid, "203006") == 3
    # 次月不同 period → 歸零。
    next_month = datetime.datetime(2030, 7, 1, tzinfo=datetime.timezone.utc)
    assert push_quota_svc.get_usage(db, tid, "203007") == 0
    assert push_quota_svc.has_push_quota(db, tid, now=next_month) is True


# ── allowance ─────────────────────────────────────────────────────────────────

def test_allowance_base_then_boost(db):
    tid = _tenant(db)
    assert push_quota_svc.allowance(db, tid) == settings.push_allowance_base
    features_svc.set_enabled(
        db, tid, features_svc.PUSH_BOOST, True, actor_user_id=None, source="admin"
    )
    assert push_quota_svc.allowance(db, tid) == (
        settings.push_allowance_base + settings.push_allowance_boost
    )


# ── has_push_quota 邊界 + consume / FOR-UPDATE upsert ──────────────────────────

def test_has_push_quota_boundary(db):
    tid = _tenant(db)
    base = settings.push_allowance_base
    # 用到剩 1：current=base-1 → 仍可再推 1。
    push_quota_svc.consume_push(db, tid, now=_NOW, n=base - 1)
    assert push_quota_svc.has_push_quota(db, tid, now=_NOW, n=1) is True
    # 補滿到 base → 已達上限，不可再推。
    push_quota_svc.consume_push(db, tid, now=_NOW, n=1)
    assert push_quota_svc.get_usage(db, tid, "203006") == base
    assert push_quota_svc.has_push_quota(db, tid, now=_NOW, n=1) is False


def test_consume_upserts_single_row(db):
    tid = _tenant(db)
    push_quota_svc.consume_push(db, tid, now=_NOW)
    push_quota_svc.consume_push(db, tid, now=_NOW)
    rows = list(
        db.execute(
            select(PushUsage).where(
                PushUsage.tenant_id == tid, PushUsage.period == "203006"
            )
        ).scalars()
    )
    assert len(rows) == 1
    assert rows[0].count == 2


def test_try_consume_stops_at_allowance(db):
    tid = _tenant(db)
    base = settings.push_allowance_base
    push_quota_svc.consume_push(db, tid, now=_NOW, n=base - 1)
    assert push_quota_svc.try_consume(db, tid, now=_NOW) is True   # 第 base 則
    assert push_quota_svc.try_consume(db, tid, now=_NOW) is False  # 超額
    assert push_quota_svc.get_usage(db, tid, "203006") == base


# ── 租戶隔離 ──────────────────────────────────────────────────────────────────

def test_tenant_isolation(db):
    a = _tenant(db)
    b = _tenant(db)
    push_quota_svc.consume_push(db, a, now=_NOW, n=10)
    assert push_quota_svc.get_usage(db, a, "203006") == 10
    assert push_quota_svc.get_usage(db, b, "203006") == 0
    assert push_quota_svc.has_push_quota(db, b, now=_NOW, n=1) is True


# ── 提醒 send loop 在額度用罄後停止送出 ─────────────────────────────────────────

def _seed_booking_tenant(db) -> int:
    tid = _tenant(db)
    cfg = LineChannelConfig(tenant_id=tid, default_target_lang="zh-TW")
    cfg.channel_secret = "s" * 32
    cfg.access_token = "a" * 40
    cfg.bot_mode = "booking"
    db.add(cfg)
    # 預約提醒需 AUTO_REMINDER；異動通知需 BOOKING_NOTIFY（預設開但顯式確保）。
    db.commit()
    return tid


def _seed_due_reminders(db, tid, n) -> None:
    slot = BookingSlot(
        tenant_id=tid,
        slot_start=_NOW + datetime.timedelta(days=1),
        max_capacity=1000,
    )
    db.add(slot)
    db.flush()
    from saas_mvp.models.reservation import RESERVATION_CONFIRMED, Reservation

    for i in range(n):
        resv = Reservation(
            tenant_id=tid,
            slot_id=slot.id,
            party_size=1,
            line_user_id=f"U{i}",
            status=RESERVATION_CONFIRMED,
        )
        db.add(resv)
        db.flush()
        db.add(
            ReservationReminder(
                tenant_id=tid,
                reservation_id=resv.id,
                line_user_id=f"U{i}",
                kind=REMINDER_DAY_BEFORE,
                remind_at=_NOW - datetime.timedelta(hours=1),  # 已到期
                status=REMINDER_PENDING,
            )
        )
    db.commit()


def test_reminder_loop_stops_at_allowance(db, monkeypatch):
    # 把 base 壓到 3 以便用少量列觸發上限（不改 production 預設語意）。
    monkeypatch.setattr(settings, "push_allowance_base", 3)
    tid = _seed_booking_tenant(db)
    _seed_due_reminders(db, tid, 5)
    fake = FakeLinePushClient()
    results = send_due_reminders(
        session_factory=_Session, push_client=fake, apply=True, now=_NOW
    )
    sent = [r for r in results if r.status == "sent"]
    skipped = [r for r in results if r.status == "skipped"]
    assert len(sent) == 3            # 只送出 allowance 則
    assert fake.call_count == 3      # 其餘未實際推播
    assert len(skipped) == 2
    assert all(r.reason == "push_allowance_exceeded" for r in skipped)
    # DB：3 sent / 2 skipped，計量列 = 3。
    rems = list(
        db.execute(
            select(ReservationReminder).where(ReservationReminder.tenant_id == tid)
        ).scalars()
    )
    assert sum(1 for r in rems if r.status == REMINDER_SENT) == 3
    assert sum(1 for r in rems if r.status == REMINDER_SKIPPED) == 2
    assert push_quota_svc.get_usage(db, tid, "203006") == 3


# ── 異動通知 send loop 在額度用罄後停止送出 ─────────────────────────────────────

def _seed_due_notifications(db, tid, n) -> None:
    from saas_mvp.models.reservation import RESERVATION_CONFIRMED, Reservation

    slot = BookingSlot(
        tenant_id=tid,
        slot_start=_NOW + datetime.timedelta(days=1),
        max_capacity=1000,
    )
    db.add(slot)
    db.flush()
    for i in range(n):
        resv = Reservation(
            tenant_id=tid,
            slot_id=slot.id,
            party_size=1,
            line_user_id=f"N{i}",
            status=RESERVATION_CONFIRMED,
        )
        db.add(resv)
        db.flush()
        db.add(
            BookingNotification(
                tenant_id=tid,
                reservation_id=resv.id,
                line_user_id=f"N{i}",
                kind="change",
                status=NOTIFY_PENDING,
                payload_text="變更通知",
                send_after=_NOW - datetime.timedelta(hours=1),
            )
        )
    db.commit()


def test_notification_loop_stops_at_allowance(db, monkeypatch):
    monkeypatch.setattr(settings, "push_allowance_base", 2)
    tid = _seed_booking_tenant(db)
    _seed_due_notifications(db, tid, 4)
    fake = FakeLinePushClient()
    results = send_due_notifications(
        session_factory=_Session, push_client=fake, apply=True, now=_NOW
    )
    assert sum(1 for r in results if r.status == "sent") == 2
    assert fake.call_count == 2
    skipped = [r for r in results if r.status == "skipped"]
    assert len(skipped) == 2
    assert all(r.reason == "push_allowance_exceeded" for r in skipped)
    notifs = list(
        db.execute(
            select(BookingNotification).where(BookingNotification.tenant_id == tid)
        ).scalars()
    )
    assert sum(1 for r in notifs if r.status == NOTIFY_SENT) == 2
    assert sum(1 for r in notifs if r.status == NOTIFY_SKIPPED) == 2
    assert push_quota_svc.get_usage(db, tid, "203006") == 2


# ── 行銷 send loop 在額度用罄後停止送出 ─────────────────────────────────────────

def test_marketing_loop_stops_at_allowance(db, monkeypatch):
    monkeypatch.setattr(settings, "push_allowance_base", 2)
    tid = _tenant(db)
    features_svc.set_enabled(
        db, tid, features_svc.MARKETING_AUTO, True, actor_user_id=None, source="admin"
    )
    for i in range(5):
        db.add(
            Customer(tenant_id=tid, line_user_id=f"M{i}", display_name=f"c{i}")
        )
    db.commit()
    camp = Campaign(
        tenant_id=tid, type=CAMPAIGN_BROADCAST, name="bc",
        message_template="hi {name}",
    )
    db.add(camp)
    db.commit()
    fake = FakeLinePushClient()
    r = marketing_svc.run_campaign(
        db, campaign=camp, now=_NOW, cap=100, push_client=fake
    )
    assert r["sent"] == 2            # 只送 allowance 則
    assert fake.call_count == 2
    sends = list(
        db.execute(
            select(CampaignSend).where(CampaignSend.campaign_id == camp.id)
        ).scalars()
    )
    assert sum(1 for s in sends if s.status == CAMPAIGN_SEND_SENT) == 2
    skipped = [s for s in sends if s.status == CAMPAIGN_SEND_SKIPPED]
    assert len(skipped) == 1  # 觸發上限時標 1 筆 skipped 後中止本活動
    assert skipped[0].last_error == "push allowance exceeded"
    assert push_quota_svc.get_usage(db, tid, "203006") == 2


# ── GET /quota/push endpoint ──────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    Base.metadata.create_all(bind=_engine)
    app = create_app()

    def override_get_db():
        s = _Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _register(client) -> tuple[str, str]:
    email = f"u_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={
        "email": email, "password": "Test1234!",
        "tenant_name": f"t_{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 201, r.text
    return email, r.json()["access_token"]


def _tenant_id_for(email) -> int:
    s = _Session()
    try:
        u = s.query(User).filter(User.email == email).first()
        return u.tenant_id
    finally:
        s.close()


def test_get_quota_push_endpoint(client):
    email, token = _register(client)
    headers = {"Authorization": f"Bearer {token}"}
    r = client.get("/quota/push", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["allowance"] == settings.push_allowance_base
    assert body["used"] == 0
    assert body["remaining"] == settings.push_allowance_base
    assert body["boost_enabled"] is False
    assert len(body["period"]) == 6  # YYYYMM

    # 消耗幾則後重查。
    tid = _tenant_id_for(email)
    s = _Session()
    try:
        push_quota_svc.consume_push(s, tid, n=5)
    finally:
        s.close()
    body2 = client.get("/quota/push", headers=headers).json()
    assert body2["used"] == 5
    assert body2["remaining"] == settings.push_allowance_base - 5


def test_get_quota_push_reflects_boost(client):
    email, token = _register(client)
    headers = {"Authorization": f"Bearer {token}"}
    tid = _tenant_id_for(email)
    s = _Session()
    try:
        features_svc.set_enabled(
            s, tid, features_svc.PUSH_BOOST, True,
            actor_user_id=None, source="admin",
        )
    finally:
        s.close()
    body = client.get("/quota/push", headers=headers).json()
    assert body["boost_enabled"] is True
    assert body["allowance"] == (
        settings.push_allowance_base + settings.push_allowance_boost
    )
