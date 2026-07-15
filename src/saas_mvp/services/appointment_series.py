"""Recurring appointments built on the existing capacity-safe booking workflow."""

from __future__ import annotations

import calendar
import datetime

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from saas_mvp.models.appointment_series import (
    OCCURRENCE_BOOKED,
    OCCURRENCE_CANCELLED,
    OCCURRENCE_CONFLICT,
    SERIES_ACTIVE,
    SERIES_CANCELLED,
    AppointmentSeries,
    AppointmentSeriesOccurrence,
)
from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.reservation import RESERVATION_CONFIRMED, Reservation
from saas_mvp.services import booking as booking_svc
from saas_mvp.services.tenants import tenant_query


MAX_OCCURRENCES = 52


class AppointmentSeriesError(ValueError):
    pass


class AppointmentSeriesNotFound(AppointmentSeriesError):
    pass


class AppointmentSeriesAlreadyExists(AppointmentSeriesError):
    pass


def _target_start(
    start: datetime.datetime, unit: str, interval: int, offset: int
) -> datetime.datetime:
    amount = interval * offset
    if unit == "week":
        return start + datetime.timedelta(weeks=amount)
    month_index = (start.month - 1) + amount
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return start.replace(year=year, month=month, day=day)


def _friendly_conflict(exc: Exception) -> str:
    if isinstance(exc, booking_svc.SlotFullError):
        return "時段或指定員工已額滿"
    if isinstance(exc, booking_svc.ResourceUnavailableError):
        return "所需房間或設備不可用"
    if isinstance(exc, booking_svc.CustomerBlacklistedError):
        return "顧客目前不可預約"
    if isinstance(exc, booking_svc.CrossTenantReferenceError):
        return "顧客、員工或服務資料已失效"
    if isinstance(exc, booking_svc.SlotNotFoundError):
        return "時段不存在或已停用"
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    return "建立預約失敗"


def _find_or_create_slot(
    db: Session,
    *,
    tenant_id: int,
    target_start: datetime.datetime,
    source_slot: BookingSlot,
    auto_create: bool,
) -> BookingSlot | None:
    slot = (
        tenant_query(db, BookingSlot, tenant_id)
        .filter(BookingSlot.slot_start == target_start)
        .first()
    )
    if slot is not None or not auto_create:
        return slot

    duration = None
    if source_slot.slot_end is not None:
        duration = source_slot.slot_end - source_slot.slot_start
    slot = BookingSlot(
        tenant_id=tenant_id,
        slot_start=target_start,
        slot_end=target_start + duration if duration is not None else None,
        max_capacity=source_slot.max_capacity,
        walkin_reserved=source_slot.walkin_reserved,
        location_id=source_slot.location_id,
        is_active=True,
    )
    db.add(slot)
    try:
        db.commit()
        db.refresh(slot)
        return slot
    except IntegrityError:
        # 另一個管理員同時建立相同時段時，使用已建立的時段即可。
        db.rollback()
        return (
            tenant_query(db, BookingSlot, tenant_id)
            .filter(BookingSlot.slot_start == target_start)
            .first()
        )


def create_from_reservation(
    db: Session,
    *,
    tenant_id: int,
    reservation_id: int,
    recurrence_unit: str,
    recurrence_interval: int,
    occurrence_count: int,
    auto_create_slots: bool,
    actor_user_id: int | None,
) -> dict:
    """Use an existing confirmed reservation as occurrence one.

    Each later occurrence is independently capacity checked through ``book_slot``.
    Its outcome is persisted, so partial conflicts are explicit and retryable rather
    than being mistaken for a complete series.
    """
    if recurrence_unit not in {"week", "month"}:
        raise AppointmentSeriesError("重複週期只支援每週或每月。")
    if not 1 <= recurrence_interval <= 12:
        raise AppointmentSeriesError("週期間隔必須介於 1～12。")
    if not 2 <= occurrence_count <= MAX_OCCURRENCES:
        raise AppointmentSeriesError(f"預約次數必須介於 2～{MAX_OCCURRENCES}。")

    source = (
        tenant_query(db, Reservation, tenant_id)
        .filter(Reservation.id == reservation_id)
        .first()
    )
    if source is None or source.status != RESERVATION_CONFIRMED:
        raise AppointmentSeriesError("只能從仍有效的預約建立重複系列。")
    existing = (
        tenant_query(db, AppointmentSeriesOccurrence, tenant_id)
        .filter(AppointmentSeriesOccurrence.reservation_id == source.id)
        .first()
    )
    if existing is not None:
        raise AppointmentSeriesAlreadyExists("此預約已屬於重複預約系列。")
    source_slot = (
        tenant_query(db, BookingSlot, tenant_id)
        .filter(BookingSlot.id == source.slot_id)
        .first()
    )
    if source_slot is None:
        raise AppointmentSeriesError("原始預約時段不存在。")

    series = AppointmentSeries(
        tenant_id=tenant_id,
        source_reservation_id=source.id,
        recurrence_unit=recurrence_unit,
        recurrence_interval=recurrence_interval,
        requested_occurrences=occurrence_count,
        status=SERIES_ACTIVE,
        created_by_user_id=actor_user_id,
    )
    db.add(series)
    db.flush()
    db.add(
        AppointmentSeriesOccurrence(
            tenant_id=tenant_id,
            series_id=series.id,
            sequence=1,
            target_start=source_slot.slot_start,
            reservation_id=source.id,
            status=OCCURRENCE_BOOKED,
        )
    )
    db.commit()
    db.refresh(series)

    booked = 1
    conflicts = 0
    for sequence in range(2, occurrence_count + 1):
        target = _target_start(
            source_slot.slot_start,
            recurrence_unit,
            recurrence_interval,
            sequence - 1,
        )
        occurrence = AppointmentSeriesOccurrence(
            tenant_id=tenant_id,
            series_id=series.id,
            sequence=sequence,
            target_start=target,
            status=OCCURRENCE_CONFLICT,
            conflict_reason="等待建立",
        )
        db.add(occurrence)
        db.commit()
        db.refresh(occurrence)
        try:
            slot = _find_or_create_slot(
                db,
                tenant_id=tenant_id,
                target_start=target,
                source_slot=source_slot,
                auto_create=auto_create_slots,
            )
            if slot is None:
                raise booking_svc.SlotNotFoundError("matching slot missing")
            booking_svc.book_slot(
                db,
                tenant_id=tenant_id,
                slot_id=slot.id,
                party_size=source.party_size,
                line_user_id=source.line_user_id,
                customer_id=source.customer_id,
                note=source.note,
                staff_id=source.staff_id,
                service_id=source.service_id,
                require_deposit=False,
                series_occurrence_id=occurrence.id,
            )
            booked += 1
        except Exception as exc:  # result is persisted; one conflict does not hide others
            db.rollback()
            if not isinstance(exc, (booking_svc.BookingError, HTTPException)):
                raise
            occurrence = (
                tenant_query(db, AppointmentSeriesOccurrence, tenant_id)
                .filter(AppointmentSeriesOccurrence.id == occurrence.id)
                .one()
            )
            occurrence.conflict_reason = _friendly_conflict(exc)
            db.commit()
            conflicts += 1
    return {"series": series, "booked": booked, "conflicts": conflicts}


def list_series(db: Session, *, tenant_id: int, limit: int = 30):
    rows = (
        tenant_query(db, AppointmentSeries, tenant_id)
        .order_by(AppointmentSeries.id.desc())
        .limit(limit)
        .all()
    )
    ids = [row.id for row in rows]
    occurrences: dict[int, list[AppointmentSeriesOccurrence]] = {}
    if ids:
        for item in (
            tenant_query(db, AppointmentSeriesOccurrence, tenant_id)
            .filter(AppointmentSeriesOccurrence.series_id.in_(ids))
            .order_by(
                AppointmentSeriesOccurrence.series_id,
                AppointmentSeriesOccurrence.sequence,
            )
            .all()
        ):
            occurrences.setdefault(item.series_id, []).append(item)
    return rows, occurrences


def cancel_from_sequence(
    db: Session, *, tenant_id: int, series_id: int, sequence_from: int
) -> int:
    series = (
        tenant_query(db, AppointmentSeries, tenant_id)
        .filter(AppointmentSeries.id == series_id)
        .first()
    )
    if series is None:
        raise AppointmentSeriesNotFound("重複預約系列不存在。")
    if sequence_from < 1:
        raise AppointmentSeriesError("取消起始序號不正確。")
    items = (
        tenant_query(db, AppointmentSeriesOccurrence, tenant_id)
        .filter(
            AppointmentSeriesOccurrence.series_id == series_id,
            AppointmentSeriesOccurrence.sequence >= sequence_from,
            AppointmentSeriesOccurrence.status != OCCURRENCE_CANCELLED,
        )
        .order_by(AppointmentSeriesOccurrence.sequence)
        .all()
    )
    cancelled = 0
    for item in items:
        if item.reservation_id is not None:
            reservation = (
                tenant_query(db, Reservation, tenant_id)
                .filter(Reservation.id == item.reservation_id)
                .first()
            )
            if reservation is not None and reservation.status == RESERVATION_CONFIRMED:
                booking_svc.cancel_reservation(
                    db, tenant_id=tenant_id, reservation_id=reservation.id
                )
                cancelled += 1
        item.status = OCCURRENCE_CANCELLED
        item.conflict_reason = None
        db.commit()
    if not (
        tenant_query(db, AppointmentSeriesOccurrence, tenant_id)
        .filter(
            AppointmentSeriesOccurrence.series_id == series_id,
            AppointmentSeriesOccurrence.status == OCCURRENCE_BOOKED,
        )
        .first()
    ):
        series.status = SERIES_CANCELLED
        db.commit()
    return cancelled


def retry_conflict(
    db: Session,
    *,
    tenant_id: int,
    series_id: int,
    occurrence_id: int,
    auto_create_slot: bool,
) -> dict:
    item = (
        tenant_query(db, AppointmentSeriesOccurrence, tenant_id)
        .filter(
            AppointmentSeriesOccurrence.id == occurrence_id,
            AppointmentSeriesOccurrence.series_id == series_id,
        )
        .first()
    )
    if item is None:
        raise AppointmentSeriesNotFound("重複預約日期不存在。")
    if item.status != OCCURRENCE_CONFLICT:
        raise AppointmentSeriesError("只有衝突中的日期可以重試。")
    series = (
        tenant_query(db, AppointmentSeries, tenant_id)
        .filter(AppointmentSeries.id == series_id)
        .first()
    )
    source = (
        tenant_query(db, Reservation, tenant_id)
        .filter(Reservation.id == series.source_reservation_id)
        .first()
        if series is not None and series.source_reservation_id is not None
        else None
    )
    source_slot = (
        tenant_query(db, BookingSlot, tenant_id)
        .filter(BookingSlot.id == source.slot_id)
        .first()
        if source is not None
        else None
    )
    if series is None or source is None or source_slot is None:
        raise AppointmentSeriesError("原始預約資料已不存在，無法重試。")
    try:
        slot = _find_or_create_slot(
            db,
            tenant_id=tenant_id,
            target_start=item.target_start,
            source_slot=source_slot,
            auto_create=auto_create_slot,
        )
        if slot is None:
            raise booking_svc.SlotNotFoundError("matching slot missing")
        created = booking_svc.book_slot(
            db,
            tenant_id=tenant_id,
            slot_id=slot.id,
            party_size=source.party_size,
            line_user_id=source.line_user_id,
            customer_id=source.customer_id,
            note=source.note,
            staff_id=source.staff_id,
            service_id=source.service_id,
            require_deposit=False,
            series_occurrence_id=item.id,
        )
    except Exception as exc:
        db.rollback()
        if not isinstance(exc, (booking_svc.BookingError, HTTPException)):
            raise
        # rollback 後重新取得 ORM instance，保存最新可操作原因。
        item = (
            tenant_query(db, AppointmentSeriesOccurrence, tenant_id)
            .filter(AppointmentSeriesOccurrence.id == occurrence_id)
            .one()
        )
        item.conflict_reason = _friendly_conflict(exc)
        db.commit()
        return {"booked": False, "reason": item.conflict_reason}
    series.status = SERIES_ACTIVE
    db.commit()
    return {"booked": True, "reservation_id": created.id}


def mark_occurrence_cancelled_in_txn(
    db: Session, *, tenant_id: int, reservation_id: int
) -> None:
    item = (
        tenant_query(db, AppointmentSeriesOccurrence, tenant_id)
        .filter(AppointmentSeriesOccurrence.reservation_id == reservation_id)
        .first()
    )
    if item is not None:
        item.status = OCCURRENCE_CANCELLED
        db.flush()
        remaining = (
            tenant_query(db, AppointmentSeriesOccurrence, tenant_id)
            .filter(
                AppointmentSeriesOccurrence.series_id == item.series_id,
                AppointmentSeriesOccurrence.status == OCCURRENCE_BOOKED,
            )
            .first()
        )
        if remaining is None:
            series = (
                tenant_query(db, AppointmentSeries, tenant_id)
                .filter(AppointmentSeries.id == item.series_id)
                .first()
            )
            if series is not None:
                series.status = SERIES_CANCELLED
