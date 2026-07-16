"""R4-B4 一鍵示範資料:載入/清除追蹤式樣本 + 開店精靈頁。"""

from __future__ import annotations

import datetime
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")
os.environ.setdefault(
    "SAAS_LINE_CHANNEL_ENCRYPT_KEY",
    "ZGV2LWxpbmUtc2VjcmV0LWtleS0zMmJ5dGVzLWxvbmc=",
)

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.customer import Customer  # noqa: E402
from saas_mvp.models.reservation import (  # noqa: E402
    RESERVATION_CONFIRMED,
    Reservation,
)
from saas_mvp.models.service import Service  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.models.tenant_demo_object import TenantDemoObject  # noqa: E402
from saas_mvp.services import demo_data as demo_svc  # noqa: E402

_NOW = datetime.datetime(2030, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)


# ── 服務層(in-memory factory)────────────────────────────────────────────────

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


def _tenant(factory) -> int:
    with factory() as db:
        t = Tenant(name="demo-tenant", plan="pro")
        db.add(t)
        db.commit()
        return t.id


def test_load_creates_tracked_objects(factory):
    tid = _tenant(factory)
    with factory() as db:
        res = demo_svc.load_demo(db, tid, now=_NOW)
    assert res == {"created": True, "service": 1, "slot": 3, "customer": 1, "reservation": 1}
    with factory() as db:
        assert db.execute(select(func.count(Service.id)).where(Service.tenant_id == tid)).scalar_one() == 1
        assert db.execute(select(func.count(BookingSlot.id)).where(BookingSlot.tenant_id == tid)).scalar_one() == 3
        assert db.execute(select(func.count(Customer.id)).where(Customer.tenant_id == tid)).scalar_one() == 1
        assert db.execute(select(func.count(Reservation.id)).where(Reservation.tenant_id == tid)).scalar_one() == 1
        # 追蹤列共 6 筆
        assert db.execute(select(func.count(TenantDemoObject.id)).where(TenantDemoObject.tenant_id == tid)).scalar_one() == 6
        # 預約落在第一個時段且 booked_count 對齊 party_size
        resv = db.execute(select(Reservation).where(Reservation.tenant_id == tid)).scalar_one()
        assert resv.status == RESERVATION_CONFIRMED
        slot = db.get(BookingSlot, resv.slot_id)
        assert slot.booked_count == resv.party_size == 2
        # 樣本皆以「示範」標示
        assert "示範" in db.execute(select(Service.name).where(Service.tenant_id == tid)).scalar_one()


def test_load_is_idempotent(factory):
    tid = _tenant(factory)
    with factory() as db:
        demo_svc.load_demo(db, tid, now=_NOW)
    with factory() as db:
        res2 = demo_svc.load_demo(db, tid, now=_NOW)
    assert res2 == {"created": False, "reason": "already_loaded"}
    with factory() as db:
        assert db.execute(select(func.count(Service.id)).where(Service.tenant_id == tid)).scalar_one() == 1


def test_has_demo_reflects_state(factory):
    tid = _tenant(factory)
    with factory() as db:
        assert demo_svc.has_demo(db, tid) is False
        demo_svc.load_demo(db, tid, now=_NOW)
    with factory() as db:
        assert demo_svc.has_demo(db, tid) is True


def test_clear_removes_everything_when_untouched(factory):
    tid = _tenant(factory)
    with factory() as db:
        demo_svc.load_demo(db, tid, now=_NOW)
    with factory() as db:
        res = demo_svc.clear_demo(db, tid)
    assert res["cleared"] is True
    assert res["removed"]["reservation"] == 1
    assert res["removed"]["slot"] == 3
    assert res["removed"]["customer"] == 1
    assert res["removed"]["service"] == 1
    assert res.get("kept") == {}
    with factory() as db:
        for model in (Service, BookingSlot, Customer, Reservation, TenantDemoObject):
            assert db.execute(select(func.count()).select_from(model).where(model.tenant_id == tid)).scalar_one() == 0
        # 清除後可再次載入
        assert demo_svc.has_demo(db, tid) is False


def test_clear_keeps_demo_slot_referenced_by_real_reservation(factory):
    tid = _tenant(factory)
    with factory() as db:
        demo_svc.load_demo(db, tid, now=_NOW)
    # 在示範時段插入一筆「真實」預約(未追蹤為 demo)
    with factory() as db:
        demo_slot = db.execute(
            select(BookingSlot).where(BookingSlot.tenant_id == tid).order_by(BookingSlot.slot_start)
        ).scalars().first()
        db.add(Reservation(
            tenant_id=tid, slot_id=demo_slot.id, party_size=1, status=RESERVATION_CONFIRMED,
        ))
        db.commit()
        kept_slot_id = demo_slot.id
    with factory() as db:
        res = demo_svc.clear_demo(db, tid)
    assert res["kept"].get("slot") == 1
    with factory() as db:
        # 被引用的示範時段保留;真實預約仍在(未被 CASCADE 刪)
        assert db.get(BookingSlot, kept_slot_id) is not None
        assert db.execute(select(func.count(Reservation.id)).where(Reservation.tenant_id == tid)).scalar_one() == 1
        # 追蹤列全清:保留物件「轉正」
        assert db.execute(select(func.count(TenantDemoObject.id)).where(TenantDemoObject.tenant_id == tid)).scalar_one() == 0


def test_clear_without_demo_is_noop(factory):
    tid = _tenant(factory)
    with factory() as db:
        res = demo_svc.clear_demo(db, tid)
    assert res == {"cleared": False, "reason": "no_demo"}


# ── 路由層(TestClient)──────────────────────────────────────────────────────

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
Base.metadata.create_all(bind=_engine)
_app = create_app()


def _override_get_db():
    db = _Session()
    try:
        yield db
    finally:
        db.close()


_app.dependency_overrides[get_db] = _override_get_db


@pytest.fixture()
def client():
    with TestClient(_app, raise_server_exceptions=True) as c:
        yield c


def _login(client) -> None:
    email = f"u_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={
        "email": email, "password": "Test1234!", "tenant_name": f"t_{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 201, r.text
    r = client.post("/ui/login", data={"email": email, "password": "Test1234!"})
    assert r.status_code == 200


def test_wizard_page_renders(client):
    _login(client)
    r = client.get("/ui/onboarding")
    assert r.status_code == 200
    assert "開店精靈" in r.text
    assert "載入示範資料" in r.text


def test_load_and_clear_via_ui(client):
    _login(client)
    # 載入(303→跟隨到精靈頁)
    r = client.post("/ui/onboarding/demo-data")
    assert r.status_code == 200
    assert "已載入示範資料" in r.text
    assert "清除示範資料" in r.text  # 狀態切為已載入
    # 「試預約」步驟應打勾(reservation_count>0)
    dash = client.get("/ui/onboarding")
    assert "✅" in dash.text
    # 清除
    r = client.post("/ui/onboarding/demo-data/clear")
    assert r.status_code == 200
    assert "已清除示範資料" in r.text
    assert "載入示範資料" in r.text  # 狀態切回未載入
