"""預約行事曆檢視測試（月曆/週曆/員工排班；對標 vibeaico）。"""

from __future__ import annotations

import datetime
import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.db import Base, get_db, import_all_models  # noqa: E402

import_all_models()

from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.services import booking as booking_svc  # noqa: E402
from saas_mvp.services import calendar_view as cal_svc  # noqa: E402
from saas_mvp.services import staff as staff_svc  # noqa: E402

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
    s = _Session()
    try:
        yield s
    finally:
        s.close()


def _tenant(db) -> int:
    t = Tenant(name=f"t_{uuid.uuid4().hex[:6]}", plan="free")
    db.add(t)
    db.flush()
    return t.id


def _slot(db, tid, when: datetime.datetime) -> int:
    slot = BookingSlot(tenant_id=tid, slot_start=when, max_capacity=10)
    db.add(slot)
    db.commit()
    return slot.id


def test_month_groups_reservations_by_date(db):
    tid = _tenant(db)
    when = datetime.datetime(2030, 7, 15, 14, 30)
    sid = _slot(db, tid, when)
    booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=2, line_user_id="Ucal")

    data = cal_svc.build_month(db, tenant_id=tid, year=2030, month=7)
    assert data["title"] == "2030 年 7 月"
    # 找出 7/15 當天格
    found = None
    for week in data["weeks"]:
        for cell in week:
            if cell["date"] == datetime.date(2030, 7, 15):
                found = cell
    assert found is not None and found["in_month"]
    assert len(found["reservations"]) == 1
    assert found["reservations"][0]["time"] == "14:30"
    assert found["reservations"][0]["status"] == "confirmed"


def test_month_nav_links(db):
    tid = _tenant(db)
    data = cal_svc.build_month(db, tenant_id=tid, year=2030, month=1)
    assert data["prev"] == "2029-12-01"
    assert data["next"] == "2030-02-01"


def test_week_groups_reservations(db):
    tid = _tenant(db)
    # 2030-07-15 是週一
    assert datetime.date(2030, 7, 15).weekday() == 0
    sid = _slot(db, tid, datetime.datetime(2030, 7, 17, 9, 0))  # 同週週三
    booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=1, line_user_id="Uw")
    data = cal_svc.build_week(db, tenant_id=tid, anchor=datetime.date(2030, 7, 15))
    assert len(data["days"]) == 7
    wed = data["days"][2]
    assert wed["date"] == datetime.date(2030, 7, 17)
    assert len(wed["reservations"]) == 1


def test_cancelled_status_shown(db):
    tid = _tenant(db)
    sid = _slot(db, tid, datetime.datetime(2030, 7, 20, 11, 0))
    resv = booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=1, line_user_id="Uc")
    booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=resv.id)
    data = cal_svc.build_month(db, tenant_id=tid, year=2030, month=7)
    statuses = [
        r["status"]
        for week in data["weeks"] for cell in week for r in cell["reservations"]
    ]
    assert "cancelled" in statuses


def test_staff_grid(db):
    tid = _tenant(db)
    s = staff_svc.create_staff(db, tenant_id=tid, name="Amy")
    staff_svc.create_shift(
        db, tenant_id=tid, staff_id=s.id, weekday=0,
        start_time="09:00", end_time="13:00", rotation="day",
    )
    grid = cal_svc.build_staff_grid(db, tenant_id=tid)
    assert len(grid["rows"]) == 1
    row = grid["rows"][0]
    assert row["name"] == "Amy"
    assert row["weekdays"][0][0]["start"] == "09:00"  # 週一有班
    assert row["weekdays"][1] == []  # 週二無班


def test_reservations_outside_month_excluded(db):
    tid = _tenant(db)
    sid = _slot(db, tid, datetime.datetime(2030, 8, 3, 10, 0))  # 8 月
    booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=1, line_user_id="Uo")
    data = cal_svc.build_month(db, tenant_id=tid, year=2030, month=7)  # 查 7 月
    total = sum(
        len(cell["reservations"])
        for week in data["weeks"] for cell in week if cell["in_month"]
    )
    assert total == 0
