"""時段硬刪（delete_slot）測試 — 無預約可刪、有預約紀錄（含已取消）擋 409、跨租戶 404。"""

from __future__ import annotations

import datetime
import os
import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.models import tenant as _t, user as _u  # noqa: F401,E402
from saas_mvp.models import customer as _c, booking_slot as _bs  # noqa: F401,E402
from saas_mvp.models import reservation as _r, reservation_reminder as _rr  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.reservation import (  # noqa: E402
    RESERVATION_CANCELLED,
    Reservation,
)
from saas_mvp.models.user import User  # noqa: E402
from saas_mvp.auth.security import decode_access_token  # noqa: E402
from saas_mvp.services import slots as slots_svc  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture(scope="module")
def client():
    Base.metadata.create_all(bind=_engine)
    app = create_app()

    def override_get_db():
        db = _Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _register(client) -> str:
    r = client.post("/auth/register", json={
        "email": f"u_{uuid.uuid4().hex[:8]}@example.com",
        "password": "Test1234!",
        "tenant_name": f"t_{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


def _tenant_id_of(token: str) -> int:
    db = _Session()
    try:
        payload = decode_access_token(token)
        return db.query(User).filter(User.id == int(payload["sub"])).first().tenant_id
    finally:
        db.close()


def _seed_slot(db, tenant_id: int) -> BookingSlot:
    slot = BookingSlot(
        tenant_id=tenant_id,
        slot_start=datetime.datetime(2030, 1, 1, 10, 0),
        max_capacity=5,
    )
    db.add(slot)
    db.commit()
    db.refresh(slot)
    return slot


class TestDeleteSlot:
    def test_delete_without_reservations(self, client):
        tid = _tenant_id_of(_register(client))
        db = _Session()
        try:
            slot = _seed_slot(db, tid)
            slots_svc.delete_slot(db, tenant_id=tid, slot_id=slot.id)
            assert (
                db.query(BookingSlot).filter(BookingSlot.id == slot.id).first()
                is None
            )
        finally:
            db.close()

    def test_delete_blocked_by_cancelled_reservation(self, client):
        """已取消的預約也是歷史紀錄 — FK 是 CASCADE，硬刪會消滅它，必須擋。"""
        tid = _tenant_id_of(_register(client))
        db = _Session()
        try:
            slot = _seed_slot(db, tid)
            db.add(Reservation(
                tenant_id=tid,
                slot_id=slot.id,
                party_size=1,
                status=RESERVATION_CANCELLED,
            ))
            db.commit()
            with pytest.raises(HTTPException) as exc:
                slots_svc.delete_slot(db, tenant_id=tid, slot_id=slot.id)
            assert exc.value.status_code == 409
            # 時段與預約紀錄都還在
            assert db.query(BookingSlot).filter(BookingSlot.id == slot.id).count() == 1
            assert (
                db.query(Reservation).filter(Reservation.slot_id == slot.id).count()
                == 1
            )
        finally:
            db.close()

    def test_delete_cross_tenant_404(self, client):
        tid_a = _tenant_id_of(_register(client))
        tid_b = _tenant_id_of(_register(client))
        db = _Session()
        try:
            slot = _seed_slot(db, tid_a)
            with pytest.raises(HTTPException) as exc:
                slots_svc.delete_slot(db, tenant_id=tid_b, slot_id=slot.id)
            assert exc.value.status_code == 404
        finally:
            db.close()
