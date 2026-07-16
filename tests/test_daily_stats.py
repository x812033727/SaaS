"""R3-B3 — daily_tenant_stats 預聚合:parity/upsert 冪等/缺日 fallback/ops。"""

from __future__ import annotations

import datetime
import uuid

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.db import Base, import_all_models
from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.daily_tenant_stat import DailyTenantStat
from saas_mvp.models.order import ORDER_PAID, Order
from saas_mvp.models.reservation import RESERVATION_CONFIRMED, Reservation
from saas_mvp.models.tenant import Tenant
from saas_mvp.ops.aggregate_daily_stats import aggregate_daily_stats
from saas_mvp.services import analytics as an
from saas_mvp.services import daily_stats

import_all_models()

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_NOW = datetime.datetime(2030, 6, 18, 12, 0, tzinfo=datetime.timezone.utc)  # 週二


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
    t = Tenant(name=f"ds_{uuid.uuid4().hex[:8]}", plan="pro")
    db.add(t)
    db.commit()
    return t.id


def _resv(db, tid, *, days_ago, status="confirmed", party=2, attended=None, user="U1"):
    start = (_NOW - datetime.timedelta(days=days_ago)).replace(tzinfo=None)
    slot = db.execute(
        select(BookingSlot).where(
            BookingSlot.tenant_id == tid, BookingSlot.slot_start == start
        )
    ).scalar_one_or_none()
    if slot is None:
        slot = BookingSlot(tenant_id=tid, slot_start=start, max_capacity=10)
        db.add(slot)
        db.flush()
    db.add(Reservation(
        tenant_id=tid, slot_id=slot.id, party_size=party, status=status,
        line_user_id=user, attended=attended,
    ))
    db.commit()


def _order(db, tid, *, days_ago, cents=50000):
    db.add(Order(
        tenant_id=tid, line_user_id="U1", status=ORDER_PAID, total_cents=cents,
        paid_at=_NOW - datetime.timedelta(days=days_ago),
    ))
    db.commit()


def _legacy_trend_series(db, *, tenant_id, period="week", periods=12, now=None):
    """B3 之前的即時實作,作為 parity 參考(逐 slot_start/paid_at 分桶)。"""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    keys = an._period_starts(now, period, periods)
    horizon = now - datetime.timedelta(days=(periods + 1) * (31 if period == "month" else 7))
    buckets = {k: {"period": k, "bookings": 0, "revenue_cents": 0} for k in keys}
    slot_rows = db.execute(
        select(BookingSlot.slot_start, func.count())
        .join(Reservation, Reservation.slot_id == BookingSlot.id)
        .where(
            Reservation.tenant_id == tenant_id,
            Reservation.status == RESERVATION_CONFIRMED,
            BookingSlot.slot_start >= horizon,
        )
        .group_by(BookingSlot.slot_start)
    ).all()
    for slot_start, cnt in slot_rows:
        k = an._period_key(slot_start, period)
        if k in buckets:
            buckets[k]["bookings"] += int(cnt)
    order_rows = db.execute(
        select(Order.paid_at, Order.total_cents).where(
            Order.tenant_id == tenant_id,
            Order.status == ORDER_PAID,
            Order.paid_at >= horizon,
        )
    ).all()
    for paid_at, cents in order_rows:
        if paid_at is None:
            continue
        k = an._period_key(paid_at, period)
        if k in buckets:
            buckets[k]["revenue_cents"] += int(cents or 0)
    return [buckets[k] for k in keys]


def _seed_mixed(db, tid):
    _resv(db, tid, days_ago=0, party=2)                      # 今天
    _resv(db, tid, days_ago=-2, party=3)                     # 本週未來(舊版也計)
    _resv(db, tid, days_ago=3, party=4, attended=True)       # 上週內
    _resv(db, tid, days_ago=3, status="cancelled", user="U2")
    _resv(db, tid, days_ago=10, party=1, attended=False, user="U3")
    _resv(db, tid, days_ago=40, party=2, user="U4")          # 幾期前
    _order(db, tid, days_ago=0, cents=30000)
    _order(db, tid, days_ago=3, cents=50000)
    _order(db, tid, days_ago=10, cents=70000)


class TestParity:
    @pytest.mark.parametrize("period", ["week", "month"])
    def test_trend_series_matches_legacy_without_rollup(self, db, period):
        """表全空(全走 fallback 即時)輸出 == 舊即時演算法。"""
        tid = _tenant(db)
        _seed_mixed(db, tid)
        assert an.trend_series(db, tenant_id=tid, period=period, now=_NOW) == \
            _legacy_trend_series(db, tenant_id=tid, period=period, now=_NOW)

    def test_trend_series_matches_legacy_after_rollup(self, db):
        """回填後(歷史走表、近日走即時)輸出仍 == 舊演算法。"""
        tid = _tenant(db)
        _seed_mixed(db, tid)
        daily_stats.rollup(db, tenant_id=tid, days_back=60, today=_NOW.date())
        db.commit()
        assert an.trend_series(db, tenant_id=tid, period="week", now=_NOW) == \
            _legacy_trend_series(db, tenant_id=tid, period="week", now=_NOW)


class TestComputeAndUpsert:
    def test_compute_day_fields(self, db):
        tid = _tenant(db)
        _seed_mixed(db, tid)
        day3 = (_NOW - datetime.timedelta(days=3)).date()
        row = daily_stats.compute_day(db, tenant_id=tid, day=day3)
        assert row["bookings_total"] == 2
        assert row["bookings_confirmed"] == 1
        assert row["bookings_cancelled"] == 1
        assert row["covers"] == 4
        assert row["attended"] == 1
        assert row["distinct_customers"] == 2  # U1 + U2
        assert row["paid_orders"] == 1 and row["revenue_cents"] == 50000

    def test_upsert_idempotent(self, db):
        tid = _tenant(db)
        _resv(db, tid, days_ago=3)
        day = (_NOW - datetime.timedelta(days=3)).date()
        daily_stats.upsert_day(db, tenant_id=tid, day=day)
        db.commit()
        daily_stats.upsert_day(db, tenant_id=tid, day=day)
        db.commit()
        rows = db.execute(
            select(DailyTenantStat).where(DailyTenantStat.tenant_id == tid)
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].bookings_confirmed == 1

    def test_rollup_excludes_today(self, db):
        tid = _tenant(db)
        _resv(db, tid, days_ago=0)
        _resv(db, tid, days_ago=1, user="U2")
        daily_stats.rollup(db, tenant_id=tid, days_back=3, today=_NOW.date())
        db.commit()
        stored = {
            r.stat_date: r for r in db.execute(
                select(DailyTenantStat).where(DailyTenantStat.tenant_id == tid)
            ).scalars()
        }
        assert _NOW.date() not in stored          # 今天不落表
        assert stored[(_NOW - datetime.timedelta(days=1)).date()].bookings_confirmed == 1


class TestDailySeriesFallback:
    def test_missing_and_today_live(self, db):
        tid = _tenant(db)
        _resv(db, tid, days_ago=0)   # 今天(未回填)
        _resv(db, tid, days_ago=2)   # 已回填日
        daily_stats.rollup(db, tenant_id=tid, days_back=3, today=_NOW.date())
        db.commit()
        # 回填後又新增一筆「今天」的預約 → 今天必須即時反映
        _resv(db, tid, days_ago=0, user="U9")
        series = daily_stats.daily_series(
            db, tenant_id=tid,
            date_from=(_NOW - datetime.timedelta(days=3)).date(),
            date_to_exclusive=(_NOW + datetime.timedelta(days=1)).date(),
            today=_NOW.date(),
        )
        assert series[_NOW.date()]["bookings_confirmed"] == 2      # 即時
        assert series[(_NOW - datetime.timedelta(days=2)).date()]["bookings_confirmed"] == 1
        assert series[(_NOW - datetime.timedelta(days=3)).date()]["bookings_confirmed"] == 0

    def test_stored_day_not_recomputed(self, db):
        """已回填的歷史日讀表:事後改資料不影響(直到下次 rollup)。"""
        tid = _tenant(db)
        _resv(db, tid, days_ago=2)
        daily_stats.rollup(db, tenant_id=tid, days_back=3, today=_NOW.date())
        db.commit()
        _resv(db, tid, days_ago=2, user="U8")  # rollup 後才加
        series = daily_stats.daily_series(
            db, tenant_id=tid,
            date_from=(_NOW - datetime.timedelta(days=2)).date(),
            date_to_exclusive=(_NOW - datetime.timedelta(days=1)).date(),
            today=_NOW.date(),
        )
        assert series[(_NOW - datetime.timedelta(days=2)).date()]["bookings_confirmed"] == 1


class TestOps:
    def test_dry_run_writes_nothing_apply_writes(self, db):
        tid = _tenant(db)
        _resv(db, tid, days_ago=1)
        factory = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
        out = aggregate_daily_stats(session_factory=factory, days=3, apply=False)
        assert out["tenants"] == 1 and out["errors"] == 0
        with factory() as s:
            assert s.execute(select(func.count(DailyTenantStat.id))).scalar() == 0
        out2 = aggregate_daily_stats(session_factory=factory, days=3, apply=True)
        assert out2["days"] == 3
        with factory() as s:
            assert s.execute(select(func.count(DailyTenantStat.id))).scalar() == 3
