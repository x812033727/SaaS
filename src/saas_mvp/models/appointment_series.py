"""Recurring appointment series and per-occurrence outcomes."""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)

from saas_mvp.db import Base


SERIES_ACTIVE = "active"
SERIES_CANCELLED = "cancelled"
SERIES_COMPLETED = "completed"

OCCURRENCE_BOOKED = "booked"
OCCURRENCE_CONFLICT = "conflict"
OCCURRENCE_CANCELLED = "cancelled"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class AppointmentSeries(Base):
    __tablename__ = "booking_appointment_series"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_reservation_id = Column(
        Integer,
        ForeignKey("booking_reservations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    recurrence_unit = Column(String(16), nullable=False)
    recurrence_interval = Column(Integer, nullable=False, server_default=text("1"))
    requested_occurrences = Column(Integer, nullable=False)
    status = Column(
        String(16), nullable=False, default=SERIES_ACTIVE, server_default=SERIES_ACTIVE
    )
    created_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("ix_booking_appointment_series_tenant_status", "tenant_id", "status"),
    )


class AppointmentSeriesOccurrence(Base):
    __tablename__ = "booking_appointment_series_occurrences"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    series_id = Column(
        Integer,
        ForeignKey("booking_appointment_series.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence = Column(Integer, nullable=False)
    target_start = Column(DateTime(timezone=True), nullable=False, index=True)
    reservation_id = Column(
        Integer,
        ForeignKey("booking_reservations.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )
    status = Column(String(16), nullable=False)
    conflict_reason = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "series_id", "sequence", name="uq_booking_appointment_series_sequence"
        ),
        Index(
            "ix_booking_appointment_occurrence_tenant_status",
            "tenant_id",
            "status",
        ),
    )
