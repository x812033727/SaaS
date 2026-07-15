"""服務套票／療程次數模型。

套票定義與顧客持有實例分離；餘額由 append-only ``PackageCreditLedger``
加總得出，避免直接覆寫餘額而失去稽核軌跡。
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)

from saas_mvp.db import Base

PACKAGE_ACTIVE = "active"
PACKAGE_CANCELLED = "cancelled"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class ServicePackage(Base):
    __tablename__ = "service_packages"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name = Column(String(128), nullable=False)
    description = Column(Text, nullable=True)
    price_cents = Column(Integer, nullable=False, default=0, server_default=text("0"))
    validity_days = Column(Integer, nullable=False, default=365, server_default=text("365"))
    is_active = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_service_package_tenant_name"),
    )


class ServicePackageItem(Base):
    __tablename__ = "service_package_items"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    package_id = Column(
        Integer, ForeignKey("service_packages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    service_id = Column(
        Integer, ForeignKey("booking_services.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    included_quantity = Column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("package_id", "service_id", name="uq_service_package_item"),
    )


class CustomerPackage(Base):
    __tablename__ = "customer_packages"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id = Column(
        Integer, ForeignKey("booking_customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    package_id = Column(
        Integer, ForeignKey("service_packages.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    package_name_snapshot = Column(String(128), nullable=False)
    price_cents_snapshot = Column(Integer, nullable=False)
    issuance_key = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False, default=PACKAGE_ACTIVE, server_default=PACKAGE_ACTIVE)
    starts_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    issued_by_user_id = Column(Integer, nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        Index("ix_customer_package_tenant_customer_status", "tenant_id", "customer_id", "status"),
        UniqueConstraint("tenant_id", "issuance_key", name="uq_customer_package_issuance_key"),
    )


class PackageCreditLedger(Base):
    __tablename__ = "package_credit_ledger"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_package_id = Column(
        Integer, ForeignKey("customer_packages.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    customer_id = Column(
        Integer, ForeignKey("booking_customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    service_id = Column(
        Integer, ForeignKey("booking_services.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    reservation_id = Column(
        Integer, ForeignKey("booking_reservations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    delta = Column(Integer, nullable=False)
    kind = Column(String(16), nullable=False)  # issue / redeem / refund / adjust
    note = Column(String(255), nullable=True)
    actor_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "reservation_id", "kind", name="uq_package_ledger_reservation_kind"
        ),
        Index(
            "ix_package_ledger_balance",
            "tenant_id",
            "customer_package_id",
            "service_id",
        ),
    )
