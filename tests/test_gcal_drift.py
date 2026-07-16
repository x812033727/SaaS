"""R4-B3 GCal 漂移偵測:輪詢 Google 端改/刪同步事件,標記+通知,不改預約。"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.db import Base
from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.email_delivery import EmailDelivery
from saas_mvp.models.reservation import RESERVATION_CONFIRMED, Reservation
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.tenant_gcal_credential import (
    GCAL_CONNECTED,
    GCAL_ERROR,
    TenantGcalCredential,
)
from saas_mvp.models.user import User
from saas_mvp.services import gcal as gcal_svc
from saas_mvp.services.gcal import StubGcalClient
from saas_mvp.services.mailer import StubMailer

_NOW = datetime.datetime(2030, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture()
def factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.drop_all(engine)
    engine.dispose()


def _setup(factory, *, status=GCAL_CONNECTED, with_event=True, future=True):
    """建租戶+已同步預約;回 (tenant_id, reservation_id, event_id)。"""
    with factory() as db:
        tenant = Tenant(name="drift", plan="pro")
        db.add(tenant)
        db.flush()
        cred = TenantGcalCredential(tenant_id=tenant.id, calendar_id="primary", status=status)
        cred.refresh_token = "enc-token"
        db.add(cred)
        db.add(User(email="owner@x.test", hashed_password="x", tenant_id=tenant.id, role="owner"))
        start = _NOW + datetime.timedelta(days=1 if future else -1)
        slot = BookingSlot(tenant_id=tenant.id, slot_start=start, max_capacity=3, booked_count=1)
        db.add(slot)
        db.flush()
        resv = Reservation(
            tenant_id=tenant.id,
            slot_id=slot.id,
            party_size=1,
            status=RESERVATION_CONFIRMED,
            gcal_event_id="evt-1" if with_event else None,
        )
        db.add(resv)
        db.commit()
        return tenant.id, resv.id, "evt-1", start


def _stub_with_event(event_id, start):
    stub = StubGcalClient()
    stub.events[event_id] = {
        "id": event_id,
        "status": "confirmed",
        "start": {"dateTime": start.astimezone(datetime.timezone.utc).isoformat()},
    }
    return stub


def test_deleted_event_marks_drift_and_notifies(factory):
    tid, rid, eid, start = _setup(factory)
    stub = StubGcalClient()  # 空 → get_event 回 None(已刪)
    mailer = StubMailer()
    with factory() as db:
        res = gcal_svc.check_drift_for_tenant(db, tid, client=stub, mailer=mailer, now=_NOW)
    assert res == {"checked": 1, "drift": 1, "cleared": 0}
    with factory() as db:
        resv = db.get(Reservation, rid)
        assert resv.gcal_drift_detected_at is not None
        assert "已被刪除" in resv.gcal_drift_note
        assert resv.status == RESERVATION_CONFIRMED  # 絕不改預約
        emails = db.execute(select(EmailDelivery)).scalars().all()
    assert len(emails) == 1
    assert emails[0].category == "gcal_drift"
    assert emails[0].recipient == "owner@x.test"


def test_time_changed_marks_drift(factory):
    tid, rid, eid, start = _setup(factory)
    moved = start + datetime.timedelta(hours=2)
    stub = _stub_with_event(eid, moved)
    with factory() as db:
        res = gcal_svc.check_drift_for_tenant(db, tid, client=stub, mailer=StubMailer(), now=_NOW)
    assert res["drift"] == 1
    with factory() as db:
        assert "時間被更改" in db.get(Reservation, rid).gcal_drift_note


def test_cancelled_status_marks_drift(factory):
    tid, rid, eid, start = _setup(factory)
    stub = _stub_with_event(eid, start)
    stub.events[eid]["status"] = "cancelled"
    with factory() as db:
        res = gcal_svc.check_drift_for_tenant(db, tid, client=stub, mailer=StubMailer(), now=_NOW)
    assert res["drift"] == 1
    with factory() as db:
        assert "取消" in db.get(Reservation, rid).gcal_drift_note


def test_consistent_event_no_drift(factory):
    tid, rid, eid, start = _setup(factory)
    stub = _stub_with_event(eid, start)
    with factory() as db:
        res = gcal_svc.check_drift_for_tenant(db, tid, client=stub, mailer=StubMailer(), now=_NOW)
    assert res == {"checked": 1, "drift": 0, "cleared": 0}
    with factory() as db:
        assert db.get(Reservation, rid).gcal_drift_detected_at is None


def test_notifies_only_once_then_clears_on_resync(factory):
    tid, rid, eid, start = _setup(factory)
    empty = StubGcalClient()
    mailer = StubMailer()
    # 第一輪:偵測到刪除,標記+寄一次
    with factory() as db:
        gcal_svc.check_drift_for_tenant(db, tid, client=empty, mailer=mailer, now=_NOW)
    # 第二輪:仍缺事件,已標記過 → 不重複寄
    with factory() as db:
        res2 = gcal_svc.check_drift_for_tenant(db, tid, client=empty, mailer=mailer, now=_NOW)
    assert res2 == {"checked": 1, "drift": 0, "cleared": 0}
    with factory() as db:
        assert len(db.execute(select(EmailDelivery)).scalars().all()) == 1
    # 第三輪:店家改回(事件一致)→ 清旗標
    stub = _stub_with_event(eid, start)
    with factory() as db:
        res3 = gcal_svc.check_drift_for_tenant(db, tid, client=stub, mailer=StubMailer(), now=_NOW)
        assert res3 == {"checked": 1, "drift": 0, "cleared": 1}
        assert db.get(Reservation, rid).gcal_drift_detected_at is None


def test_unconnected_tenant_is_noop(factory):
    tid, rid, eid, start = _setup(factory, status=GCAL_ERROR)
    with factory() as db:
        res = gcal_svc.check_drift_for_tenant(db, tid, client=StubGcalClient(), mailer=StubMailer(), now=_NOW)
    assert res == {"checked": 0, "drift": 0, "cleared": 0}


def test_past_reservations_ignored(factory):
    tid, rid, eid, start = _setup(factory, future=False)
    with factory() as db:
        res = gcal_svc.check_drift_for_tenant(db, tid, client=StubGcalClient(), mailer=StubMailer(), now=_NOW)
    assert res["checked"] == 0


def test_dry_run_counts_without_side_effects(factory):
    tid, rid, eid, start = _setup(factory)
    mailer = StubMailer()
    with factory() as db:
        res = gcal_svc.check_drift_for_tenant(
            db, tid, client=StubGcalClient(), mailer=mailer, now=_NOW, apply=False
        )
    assert res == {"checked": 1, "drift": 1, "cleared": 0}  # 有偵測到
    with factory() as db:  # 但未落庫、未寄信
        assert db.get(Reservation, rid).gcal_drift_detected_at is None
        assert db.execute(select(EmailDelivery)).scalars().all() == []
        cred = db.execute(
            select(TenantGcalCredential).where(TenantGcalCredential.tenant_id == tid)
        ).scalar_one()
        assert cred.last_drift_check_at is None
    assert mailer.sent == []


def test_updates_last_drift_check_at(factory):
    tid, rid, eid, start = _setup(factory)
    stub = _stub_with_event(eid, start)
    with factory() as db:
        gcal_svc.check_drift_for_tenant(db, tid, client=stub, mailer=StubMailer(), now=_NOW)
    with factory() as db:
        cred = db.execute(
            select(TenantGcalCredential).where(TenantGcalCredential.tenant_id == tid)
        ).scalar_one()
        assert cred.last_drift_check_at is not None
