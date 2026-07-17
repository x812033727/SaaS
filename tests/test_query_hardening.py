"""R6-C2 — 查詢硬化:staff-grid N+1 消除、list_reservations newest_first。"""

from __future__ import annotations

import datetime
import os
import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.db import Base, import_all_models  # noqa: E402

import_all_models()

from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.services import booking as booking_svc  # noqa: E402
from saas_mvp.services import calendar_view as cal_svc  # noqa: E402
from saas_mvp.services import staff as staff_svc  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
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


class _SelectCounter:
    """統計 SELECT 執行次數(N+1 回歸守門)。"""

    def __init__(self, engine):
        self.engine = engine
        self.count = 0

    def _on(self, conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            self.count += 1

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *a):
        event.remove(self.engine, "before_cursor_execute", self._on)


class TestStaffGridNoNPlusOne:
    def _seed_staff_with_shifts(self, db, tid, n_staff, shifts_each):
        for i in range(n_staff):
            s = staff_svc.create_staff(db, tenant_id=tid, name=f"S{i}")
            for w in range(shifts_each):
                staff_svc.create_shift(
                    db, tenant_id=tid, staff_id=s.id, weekday=w % 7,
                    start_time="09:00", end_time="13:00", rotation="day",
                )

    def test_query_count_flat_with_staff_count(self, db):
        tid = _tenant(db)
        self._seed_staff_with_shifts(db, tid, n_staff=5, shifts_each=3)
        with _SelectCounter(_engine) as c1:
            grid = cal_svc.build_staff_grid(db, tenant_id=tid)
        assert len(grid["rows"]) == 5
        base = c1.count

        # 員工數翻倍,查詢數不應隨之線性增長(N+1 已消除;固定 2 個主查詢)。
        tid2 = _tenant(db)
        self._seed_staff_with_shifts(db, tid2, n_staff=10, shifts_each=3)
        with _SelectCounter(_engine) as c2:
            grid2 = cal_svc.build_staff_grid(db, tenant_id=tid2)
        assert len(grid2["rows"]) == 10
        # 10 員工的查詢數不超過 5 員工的(舊 N+1 會是 ~2x)
        assert c2.count <= base

    def test_grouping_correct(self, db):
        tid = _tenant(db)
        s = staff_svc.create_staff(db, tenant_id=tid, name="Amy")
        staff_svc.create_shift(
            db, tenant_id=tid, staff_id=s.id, weekday=0,
            start_time="09:00", end_time="13:00", rotation="day",
        )
        staff_svc.create_shift(
            db, tenant_id=tid, staff_id=s.id, weekday=2,
            start_time="14:00", end_time="18:00", rotation="day",
        )
        grid = cal_svc.build_staff_grid(db, tenant_id=tid)
        row = grid["rows"][0]
        assert row["weekdays"][0][0]["start"] == "09:00"
        assert row["weekdays"][2][0]["start"] == "14:00"
        assert row["weekdays"][1] == []

    def test_inactive_shift_excluded(self, db):
        tid = _tenant(db)
        s = staff_svc.create_staff(db, tenant_id=tid, name="Bob")
        sh = staff_svc.create_shift(
            db, tenant_id=tid, staff_id=s.id, weekday=0,
            start_time="09:00", end_time="13:00", rotation="day",
        )
        grouped = staff_svc.active_shifts_by_staff(db, tenant_id=tid)
        assert len(grouped[s.id]) == 1
        # 停用班表不進分組
        sh.is_active = False
        db.commit()
        grouped2 = staff_svc.active_shifts_by_staff(db, tenant_id=tid)
        assert grouped2.get(s.id, []) == []


class TestListReservationsNewestFirst:
    def _slot(self, db, tid, when):
        slot = BookingSlot(tenant_id=tid, slot_start=when, max_capacity=10)
        db.add(slot)
        db.commit()
        return slot.id

    def test_newest_first_limit(self, db):
        tid = _tenant(db)
        ids = []
        for i in range(5):
            sid = self._slot(db, tid, datetime.datetime(2030, 7, 10 + i, 10, 0))
            r = booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=1,
                                      line_user_id="Urep")
            ids.append(r.id)
        # newest_first + limit 3 → 最新 3 筆(id 大→小)
        rows = booking_svc.list_reservations(
            db, tenant_id=tid, line_user_id="Urep", limit=3, newest_first=True
        )
        assert [r.id for r in rows] == list(reversed(ids))[:3]

    def test_default_still_ascending(self, db):
        tid = _tenant(db)
        ids = []
        for i in range(3):
            sid = self._slot(db, tid, datetime.datetime(2030, 8, 10 + i, 10, 0))
            r = booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=1,
                                      line_user_id="Uasc")
            ids.append(r.id)
        rows = booking_svc.list_reservations(db, tenant_id=tid, line_user_id="Uasc")
        assert [r.id for r in rows] == ids  # 預設 id 升冪不變
