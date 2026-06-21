"""預約容量控管測試（service 直連 DB）。

重點：原子容量檢查、walk-in 保留名額、超量擋下（含正常控制組）、取消回補。
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# 確保 model metadata 載入
from saas_mvp.models import tenant as _t  # noqa: F401
from saas_mvp.models import customer as _c  # noqa: F401
from saas_mvp.models import booking_slot as _bs  # noqa: F401
from saas_mvp.models import reservation as _r  # noqa: F401
from saas_mvp.models import reservation_reminder as _rr  # noqa: F401

from saas_mvp.db import Base
from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.customer import Customer
from saas_mvp.models.reservation import RESERVATION_CANCELLED, Reservation
from saas_mvp.models.tenant import Tenant
from saas_mvp.services import booking as booking_svc

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    session = _Session()
    try:
        yield session
    finally:
        session.close()


def _seed_tenant(db) -> int:
    t = Tenant(name="cap_test", plan="free")
    db.add(t)
    db.commit()
    return t.id


def _seed_slot(db, tenant_id, *, max_capacity=2, walkin_reserved=0) -> int:
    start = datetime.datetime(2030, 1, 1, 18, 0, tzinfo=datetime.timezone.utc)
    slot = BookingSlot(
        tenant_id=tenant_id,
        slot_start=start,
        max_capacity=max_capacity,
        walkin_reserved=walkin_reserved,
    )
    db.add(slot)
    db.commit()
    return slot.id


class TestCapacity:
    def test_book_within_capacity_succeeds(self, db):
        """控制組：容量足夠可訂，booked_count 遞增。"""
        tid = _seed_tenant(db)
        sid = _seed_slot(db, tid, max_capacity=3)
        resv = booking_svc.book_slot(
            db, tenant_id=tid, slot_id=sid, party_size=2, line_user_id="U1"
        )
        assert resv.status == "confirmed"
        slot = db.get(BookingSlot, sid)
        assert slot.booked_count == 2
        assert slot.online_available == 1

    def test_overbook_raises_slot_full(self, db):
        """拒絕組：超過容量回 SlotFullError，booked_count 不超賣。"""
        tid = _seed_tenant(db)
        sid = _seed_slot(db, tid, max_capacity=2)
        booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=2, line_user_id="U1")
        with pytest.raises(booking_svc.SlotFullError):
            booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=1, line_user_id="U2")
        slot = db.get(BookingSlot, sid)
        assert slot.booked_count == 2  # 未超賣

    def test_walkin_reserved_withheld_from_online(self, db):
        """walk-in 保留名額不開放線上：max=5 reserved=2 → 線上最多 3。"""
        tid = _seed_tenant(db)
        sid = _seed_slot(db, tid, max_capacity=5, walkin_reserved=2)
        booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=3, line_user_id="U1")
        slot = db.get(BookingSlot, sid)
        assert slot.online_available == 0
        with pytest.raises(booking_svc.SlotFullError):
            booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=1, line_user_id="U2")

    def test_sequential_books_serialized(self, db):
        """1 容量時段：第一筆成功、第二筆擋下（鎖內重驗）。"""
        tid = _seed_tenant(db)
        sid = _seed_slot(db, tid, max_capacity=1)
        booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=1, line_user_id="U1")
        with pytest.raises(booking_svc.SlotFullError):
            booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=1, line_user_id="U2")

    def test_inactive_slot_not_found(self, db):
        tid = _seed_tenant(db)
        sid = _seed_slot(db, tid, max_capacity=2)
        slot = db.get(BookingSlot, sid)
        slot.is_active = False
        db.commit()
        with pytest.raises(booking_svc.SlotNotFoundError):
            booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=1, line_user_id="U1")

    def test_cross_tenant_slot_not_found(self, db):
        tid = _seed_tenant(db)
        other = Tenant(name="other", plan="free")
        db.add(other)
        db.commit()
        sid = _seed_slot(db, tid, max_capacity=2)
        with pytest.raises(booking_svc.SlotNotFoundError):
            booking_svc.book_slot(
                db, tenant_id=other.id, slot_id=sid, party_size=1, line_user_id="U1"
            )


class TestCancel:
    def test_cancel_restores_capacity(self, db):
        tid = _seed_tenant(db)
        sid = _seed_slot(db, tid, max_capacity=2)
        resv = booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=2, line_user_id="U1")
        booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=resv.id)
        slot = db.get(BookingSlot, sid)
        assert slot.booked_count == 0
        # 控制組：回補後可重訂
        again = booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=2, line_user_id="U2")
        assert again.status == "confirmed"

    def test_double_cancel_idempotent(self, db):
        tid = _seed_tenant(db)
        sid = _seed_slot(db, tid, max_capacity=2)
        resv = booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=2, line_user_id="U1")
        booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=resv.id)
        booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=resv.id)
        slot = db.get(BookingSlot, sid)
        assert slot.booked_count == 0  # 不重複回補成負

    def test_cancel_wrong_line_user_rejected(self, db):
        tid = _seed_tenant(db)
        sid = _seed_slot(db, tid, max_capacity=2)
        resv = booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=1, line_user_id="U1")
        with pytest.raises(booking_svc.ReservationPermissionError):
            booking_svc.cancel_reservation(
                db, tenant_id=tid, reservation_id=resv.id, line_user_id="U_other"
            )
        # 控制組：正確使用者可取消
        booking_svc.cancel_reservation(
            db, tenant_id=tid, reservation_id=resv.id, line_user_id="U1"
        )
        assert db.get(Reservation, resv.id).status == RESERVATION_CANCELLED


class TestCustomerAutoCreate:
    def test_customer_created_and_bumped(self, db):
        tid = _seed_tenant(db)
        sid = _seed_slot(db, tid, max_capacity=5)
        booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=1, line_user_id="Uabc", display_name="阿明")
        booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=1, line_user_id="Uabc")
        customers = db.query(Customer).filter(Customer.tenant_id == tid).all()
        assert len(customers) == 1
        assert customers[0].line_user_id == "Uabc"
        assert customers[0].booking_count == 2
        assert customers[0].display_name == "阿明"
