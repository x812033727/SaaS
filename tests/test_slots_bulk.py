"""批次產生時段（bulk_generate_slots）測試 — 展開數量、間隔、星期過濾、
重複略過、參數驗證、上限防呆。"""

from __future__ import annotations

import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.models import tenant as _t  # noqa: F401
from saas_mvp.models import booking_slot as _bs  # noqa: F401

from saas_mvp.db import Base
from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.tenant import Tenant
from saas_mvp.services import slots as slots_svc

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
    t = Tenant(name="bulk_test", plan="free")
    db.add(t)
    db.commit()
    return t.id


def _gen(db, tid, **over):
    kw = dict(
        date_start=datetime.date(2026, 7, 1),
        date_end=datetime.date(2026, 7, 1),
        time_start=datetime.time(11, 0),
        time_end=datetime.time(14, 0),
        interval_minutes=60,
        max_capacity=10,
    )
    kw.update(over)
    return slots_svc.bulk_generate_slots(db, tenant_id=tid, **kw)


class TestExpansion:
    def test_single_day_hourly(self, db):
        tid = _tenant(db)
        res = _gen(db, tid)  # 11,12,13（14 不含）
        assert res == {"created": 3, "skipped": 0, "total": 3}
        rows = db.query(BookingSlot).filter_by(tenant_id=tid).order_by(
            BookingSlot.slot_start
        ).all()
        assert [r.slot_start.hour for r in rows] == [11, 12, 13]
        # slot_end = start + interval
        assert rows[0].slot_end.hour == 12
        assert all(r.max_capacity == 10 for r in rows)

    def test_interval_30min(self, db):
        tid = _tenant(db)
        res = _gen(db, tid, interval_minutes=30)  # 11:00,11:30,12:00,12:30,13:00,13:30
        assert res["created"] == 6

    def test_multi_day_range(self, db):
        tid = _tenant(db)
        res = _gen(
            db, tid,
            date_start=datetime.date(2026, 7, 1),
            date_end=datetime.date(2026, 7, 3),
        )  # 3 天 × 3 格
        assert res["created"] == 9

    def test_walkin_reserved_applied(self, db):
        tid = _tenant(db)
        _gen(db, tid, max_capacity=10, walkin_reserved=3)
        row = db.query(BookingSlot).filter_by(tenant_id=tid).first()
        assert row.walkin_reserved == 3
        assert row.online_available == 7


class TestWeekdayFilter:
    def test_only_selected_weekdays(self, db):
        tid = _tenant(db)
        # 2026-07-01 是週三(2)。範圍含週三~週五，限定週三+週五。
        res = _gen(
            db, tid,
            date_start=datetime.date(2026, 7, 1),  # Wed
            date_end=datetime.date(2026, 7, 5),  # Sun
            weekdays={2, 4},  # Wed, Fri
        )
        # Wed(7/1)+Fri(7/3) 各 3 格 = 6
        assert res["created"] == 6
        weekdays_seen = {
            r.slot_start.weekday()
            for r in db.query(BookingSlot).filter_by(tenant_id=tid)
        }
        assert weekdays_seen == {2, 4}

    def test_empty_weekdays_means_all(self, db):
        tid = _tenant(db)
        res = _gen(
            db, tid,
            date_start=datetime.date(2026, 7, 1),
            date_end=datetime.date(2026, 7, 2),
            weekdays=None,
        )
        assert res["created"] == 6  # 2 天 × 3


class TestIdempotentSkip:
    def test_rerun_skips_existing(self, db):
        tid = _tenant(db)
        first = _gen(db, tid)
        assert first["created"] == 3
        second = _gen(db, tid)  # 同參數重跑
        assert second == {"created": 0, "skipped": 3, "total": 3}
        # 沒有重複列
        assert db.query(BookingSlot).filter_by(tenant_id=tid).count() == 3

    def test_partial_overlap(self, db):
        tid = _tenant(db)
        _gen(db, tid, time_start=datetime.time(11, 0), time_end=datetime.time(13, 0))  # 11,12
        res = _gen(
            db, tid, time_start=datetime.time(12, 0), time_end=datetime.time(15, 0)
        )  # 12(已存在),13,14
        assert res["created"] == 2
        assert res["skipped"] == 1


class TestValidation:
    def test_reversed_date_range(self, db):
        tid = _tenant(db)
        with pytest.raises(HTTPException) as ei:
            _gen(
                db, tid,
                date_start=datetime.date(2026, 7, 5),
                date_end=datetime.date(2026, 7, 1),
            )
        assert ei.value.status_code == 422

    def test_reversed_time_range(self, db):
        tid = _tenant(db)
        with pytest.raises(HTTPException):
            _gen(db, tid, time_start=datetime.time(14, 0), time_end=datetime.time(11, 0))

    def test_zero_interval(self, db):
        tid = _tenant(db)
        with pytest.raises(HTTPException):
            _gen(db, tid, interval_minutes=0)

    def test_walkin_exceeds_capacity(self, db):
        tid = _tenant(db)
        with pytest.raises(HTTPException):
            _gen(db, tid, max_capacity=5, walkin_reserved=6)

    def test_exceeds_max_bulk(self, db):
        tid = _tenant(db)
        # 每分鐘一格 × 長區間 → 超過 MAX_BULK_SLOTS
        with pytest.raises(HTTPException) as ei:
            _gen(
                db, tid,
                date_start=datetime.date(2026, 7, 1),
                date_end=datetime.date(2026, 12, 31),
                time_start=datetime.time(0, 0),
                time_end=datetime.time(23, 59),
                interval_minutes=1,
            )
        assert ei.value.status_code == 422
        # 失敗時不應殘留任何列
        assert db.query(BookingSlot).filter_by(tenant_id=tid).count() == 0
