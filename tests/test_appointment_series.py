"""Recurring appointment series: generation, conflicts and following cancellation."""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.db import Base
from saas_mvp.models.appointment_series import (
    OCCURRENCE_BOOKED,
    OCCURRENCE_CANCELLED,
    OCCURRENCE_CONFLICT,
    SERIES_CANCELLED,
    AppointmentSeries,
    AppointmentSeriesOccurrence,
)
from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.customer import Customer
from saas_mvp.models.reservation import RESERVATION_CANCELLED, Reservation
from saas_mvp.models.tenant import Tenant
from saas_mvp.services import appointment_series as series_svc
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
    with _Session() as session:
        yield session


def _source(db, *, start: datetime.datetime, capacity: int = 2):
    tenant = Tenant(name="series-test", plan="free")
    db.add(tenant)
    db.commit()
    slot = BookingSlot(
        tenant_id=tenant.id,
        slot_start=start,
        slot_end=start + datetime.timedelta(hours=1),
        max_capacity=capacity,
        location_id=None,
    )
    db.add(slot)
    db.commit()
    reservation = booking_svc.book_slot(
        db,
        tenant_id=tenant.id,
        slot_id=slot.id,
        line_user_id="Useries",
        display_name="週期顧客",
        party_size=1,
        note="四週療程",
    )
    return tenant, slot, reservation


def test_weekly_series_auto_creates_slots_and_reservations(db):
    start = datetime.datetime(2032, 1, 5, 10, tzinfo=datetime.timezone.utc)
    tenant, _, source = _source(db, start=start)

    result = series_svc.create_from_reservation(
        db,
        tenant_id=tenant.id,
        reservation_id=source.id,
        recurrence_unit="week",
        recurrence_interval=2,
        occurrence_count=4,
        auto_create_slots=True,
        actor_user_id=10,
    )

    assert result["booked"] == 4
    assert result["conflicts"] == 0
    items = db.query(AppointmentSeriesOccurrence).order_by(
        AppointmentSeriesOccurrence.sequence
    ).all()
    assert [item.status for item in items] == [OCCURRENCE_BOOKED] * 4
    assert [item.target_start.date() for item in items] == [
        datetime.date(2032, 1, 5),
        datetime.date(2032, 1, 19),
        datetime.date(2032, 2, 2),
        datetime.date(2032, 2, 16),
    ]
    assert db.query(BookingSlot).count() == 4
    assert db.query(Reservation).count() == 4
    assert db.query(Customer).one().booking_count == 4


def test_monthly_series_clamps_end_of_month(db):
    start = datetime.datetime(2031, 1, 31, 9, tzinfo=datetime.timezone.utc)
    tenant, _, source = _source(db, start=start)
    series_svc.create_from_reservation(
        db,
        tenant_id=tenant.id,
        reservation_id=source.id,
        recurrence_unit="month",
        recurrence_interval=1,
        occurrence_count=3,
        auto_create_slots=True,
        actor_user_id=None,
    )
    items = db.query(AppointmentSeriesOccurrence).order_by(
        AppointmentSeriesOccurrence.sequence
    ).all()
    assert [item.target_start.date() for item in items] == [
        datetime.date(2031, 1, 31),
        datetime.date(2031, 2, 28),
        datetime.date(2031, 3, 31),
    ]


def test_conflict_is_recorded_while_other_occurrences_continue(db):
    start = datetime.datetime(2032, 3, 1, 14, tzinfo=datetime.timezone.utc)
    tenant, _, source = _source(db, start=start)
    blocked_slot = BookingSlot(
        tenant_id=tenant.id,
        slot_start=start + datetime.timedelta(weeks=1),
        slot_end=start + datetime.timedelta(weeks=1, hours=1),
        max_capacity=1,
    )
    db.add(blocked_slot)
    db.commit()
    booking_svc.book_slot(
        db,
        tenant_id=tenant.id,
        slot_id=blocked_slot.id,
        line_user_id="Uother",
    )

    result = series_svc.create_from_reservation(
        db,
        tenant_id=tenant.id,
        reservation_id=source.id,
        recurrence_unit="week",
        recurrence_interval=1,
        occurrence_count=3,
        auto_create_slots=True,
        actor_user_id=1,
    )

    assert result["booked"] == 2
    assert result["conflicts"] == 1
    items = db.query(AppointmentSeriesOccurrence).order_by(
        AppointmentSeriesOccurrence.sequence
    ).all()
    assert [item.status for item in items] == [
        OCCURRENCE_BOOKED,
        OCCURRENCE_CONFLICT,
        OCCURRENCE_BOOKED,
    ]
    assert items[1].conflict_reason == "時段或指定員工已額滿"


def test_cancel_from_sequence_cancels_only_that_and_following(db):
    start = datetime.datetime(2032, 4, 1, 11, tzinfo=datetime.timezone.utc)
    tenant, _, source = _source(db, start=start)
    result = series_svc.create_from_reservation(
        db,
        tenant_id=tenant.id,
        reservation_id=source.id,
        recurrence_unit="week",
        recurrence_interval=1,
        occurrence_count=4,
        auto_create_slots=True,
        actor_user_id=1,
    )
    series_id = result["series"].id

    assert series_svc.cancel_from_sequence(
        db, tenant_id=tenant.id, series_id=series_id, sequence_from=3
    ) == 2
    items = db.query(AppointmentSeriesOccurrence).order_by(
        AppointmentSeriesOccurrence.sequence
    ).all()
    assert [item.status for item in items] == [
        OCCURRENCE_BOOKED,
        OCCURRENCE_BOOKED,
        OCCURRENCE_CANCELLED,
        OCCURRENCE_CANCELLED,
    ]
    assert all(
        db.get(Reservation, item.reservation_id).status == RESERVATION_CANCELLED
        for item in items[2:]
    )
    assert db.get(AppointmentSeries, series_id).status != SERIES_CANCELLED


def test_regular_single_cancel_updates_series_occurrence(db):
    start = datetime.datetime(2032, 5, 1, 11, tzinfo=datetime.timezone.utc)
    tenant, _, source = _source(db, start=start)
    result = series_svc.create_from_reservation(
        db,
        tenant_id=tenant.id,
        reservation_id=source.id,
        recurrence_unit="week",
        recurrence_interval=1,
        occurrence_count=2,
        auto_create_slots=True,
        actor_user_id=1,
    )
    second = (
        db.query(AppointmentSeriesOccurrence)
        .filter_by(series_id=result["series"].id, sequence=2)
        .one()
    )
    booking_svc.cancel_reservation(
        db, tenant_id=tenant.id, reservation_id=second.reservation_id
    )
    db.refresh(second)
    assert second.status == OCCURRENCE_CANCELLED


def test_conflict_can_be_retried_after_capacity_is_released(db):
    start = datetime.datetime(2032, 5, 20, 11, tzinfo=datetime.timezone.utc)
    tenant, _, source = _source(db, start=start)
    target_slot = BookingSlot(
        tenant_id=tenant.id,
        slot_start=start + datetime.timedelta(weeks=1),
        slot_end=start + datetime.timedelta(weeks=1, hours=1),
        max_capacity=1,
    )
    db.add(target_slot)
    db.commit()
    blocker = booking_svc.book_slot(
        db, tenant_id=tenant.id, slot_id=target_slot.id, line_user_id="Ublocker"
    )
    result = series_svc.create_from_reservation(
        db,
        tenant_id=tenant.id,
        reservation_id=source.id,
        recurrence_unit="week",
        recurrence_interval=1,
        occurrence_count=2,
        auto_create_slots=True,
        actor_user_id=1,
    )
    conflict = (
        db.query(AppointmentSeriesOccurrence)
        .filter_by(series_id=result["series"].id, sequence=2)
        .one()
    )
    booking_svc.cancel_reservation(
        db, tenant_id=tenant.id, reservation_id=blocker.id
    )

    retried = series_svc.retry_conflict(
        db,
        tenant_id=tenant.id,
        series_id=result["series"].id,
        occurrence_id=conflict.id,
        auto_create_slot=False,
    )
    assert retried["booked"] is True
    db.refresh(conflict)
    assert conflict.status == OCCURRENCE_BOOKED
    assert conflict.reservation_id is not None
    assert conflict.conflict_reason is None


def test_cancel_following_also_closes_unresolved_conflicts(db):
    start = datetime.datetime(2032, 5, 25, 11, tzinfo=datetime.timezone.utc)
    tenant, _, source = _source(db, start=start)
    result = series_svc.create_from_reservation(
        db,
        tenant_id=tenant.id,
        reservation_id=source.id,
        recurrence_unit="week",
        recurrence_interval=1,
        occurrence_count=3,
        auto_create_slots=False,
        actor_user_id=1,
    )
    assert result["conflicts"] == 2
    assert series_svc.cancel_from_sequence(
        db,
        tenant_id=tenant.id,
        series_id=result["series"].id,
        sequence_from=2,
    ) == 0
    later = (
        db.query(AppointmentSeriesOccurrence)
        .filter(AppointmentSeriesOccurrence.sequence >= 2)
        .all()
    )
    assert later and all(item.status == OCCURRENCE_CANCELLED for item in later)


def test_source_reservation_cannot_join_two_series(db):
    start = datetime.datetime(2032, 6, 1, 11, tzinfo=datetime.timezone.utc)
    tenant, _, source = _source(db, start=start)
    kwargs = dict(
        tenant_id=tenant.id,
        reservation_id=source.id,
        recurrence_unit="week",
        recurrence_interval=1,
        occurrence_count=2,
        auto_create_slots=True,
        actor_user_id=1,
    )
    series_svc.create_from_reservation(db, **kwargs)
    with pytest.raises(series_svc.AppointmentSeriesAlreadyExists):
        series_svc.create_from_reservation(db, **kwargs)
