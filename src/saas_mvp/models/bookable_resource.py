"""服務所需房間／設備資源，以及不可變的預約配置紀錄。"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)

from saas_mvp.db import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class ResourceType(Base):
    __tablename__ = "booking_resource_types"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(128), nullable=False)
    description = Column(Text, nullable=True)
    is_active = Column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "name", name="uq_booking_resource_type_name"
        ),
    )


class BookableResource(Base):
    __tablename__ = "booking_resources"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    resource_type_id = Column(
        Integer,
        ForeignKey("booking_resource_types.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    location_id = Column(
        Integer,
        ForeignKey("booking_locations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    name = Column(String(128), nullable=False)
    description = Column(Text, nullable=True)
    internal_code = Column(String(64), nullable=True)
    capacity = Column(Integer, nullable=False, default=1, server_default=text("1"))
    available_from = Column(Date, nullable=True)
    available_until = Column(Date, nullable=True)
    is_active = Column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_booking_resource_name"),
        UniqueConstraint(
            "tenant_id", "internal_code", name="uq_booking_resource_internal_code"
        ),
    )


class ResourceAvailability(Base):
    __tablename__ = "booking_resource_availabilities"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    resource_id = Column(
        Integer,
        ForeignKey("booking_resources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    weekday = Column(Integer, nullable=False)
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "resource_id",
            "weekday",
            "start_time",
            "end_time",
            name="uq_booking_resource_availability_window",
        ),
        Index(
            "ix_booking_resource_availability_lookup",
            "tenant_id",
            "resource_id",
            "weekday",
        ),
    )


class ResourceBlock(Base):
    __tablename__ = "booking_resource_blocks"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    resource_id = Column(
        Integer,
        ForeignKey("booking_resources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    starts_at = Column(DateTime(timezone=True), nullable=False)
    ends_at = Column(DateTime(timezone=True), nullable=False)
    reason = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        Index(
            "ix_booking_resource_block_overlap",
            "tenant_id",
            "resource_id",
            "starts_at",
            "ends_at",
        ),
    )


class ServiceResourceRequirement(Base):
    __tablename__ = "booking_service_resource_requirements"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    service_id = Column(
        Integer,
        ForeignKey("booking_services.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    resource_type_id = Column(
        Integer,
        ForeignKey("booking_resource_types.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    quantity = Column(Integer, nullable=False, default=1, server_default=text("1"))

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "service_id",
            "resource_type_id",
            name="uq_booking_service_resource_requirement",
        ),
    )


class ReservationResourceAllocation(Base):
    __tablename__ = "booking_reservation_resource_allocations"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reservation_id = Column(
        Integer,
        ForeignKey("booking_reservations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    resource_id = Column(
        Integer,
        ForeignKey("booking_resources.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    resource_type_id = Column(
        Integer,
        ForeignKey("booking_resource_types.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    quantity = Column(Integer, nullable=False, default=1, server_default=text("1"))
    starts_at = Column(DateTime(timezone=True), nullable=False)
    ends_at = Column(DateTime(timezone=True), nullable=False)
    resource_name_snapshot = Column(String(128), nullable=False)
    resource_type_name_snapshot = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint(
            "reservation_id",
            "resource_id",
            name="uq_booking_reservation_resource_allocation",
        ),
        Index(
            "ix_booking_resource_allocation_overlap",
            "tenant_id",
            "resource_id",
            "starts_at",
            "ends_at",
        ),
    )
