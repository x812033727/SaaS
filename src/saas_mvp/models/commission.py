"""員工抽成與薪資結算模型。

成交抽成以 ``CommissionEarning`` 保存不可變快照；規則日後調整不會回寫歷史。
取消已付訂單時保留原紀錄並建立負數沖銷，讓已結算薪資仍可稽核。
"""

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
    UniqueConstraint,
    text,
)

from saas_mvp.db import Base

ITEM_SERVICE = "service"
ITEM_PRODUCT = "product"
ITEM_TIP = "tip"
ITEM_REVERSAL = "reversal"
VALID_RULE_ITEM_TYPES = frozenset({ITEM_SERVICE, ITEM_PRODUCT})

METHOD_PERCENT = "percent"
METHOD_FIXED = "fixed"
VALID_METHODS = frozenset({METHOD_PERCENT, METHOD_FIXED})

BASIS_NET = "net"
BASIS_GROSS = "gross"
VALID_BASES = frozenset({BASIS_NET, BASIS_GROSS})

PAY_RUN_DRAFT = "draft"
PAY_RUN_FINALIZED = "finalized"
PAY_RUN_PAID = "paid"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class CommissionRule(Base):
    __tablename__ = "staff_commission_rules"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    staff_id = Column(
        Integer,
        ForeignKey("booking_staff.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    item_type = Column(String(16), nullable=False)
    method = Column(String(16), nullable=False)
    # percent = basis points（1000 = 10%）；fixed = 每件固定 cents。
    value = Column(Integer, nullable=False)
    calculation_basis = Column(
        String(16), nullable=False, default=BASIS_NET, server_default=BASIS_NET
    )
    effective_from = Column(Date, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    created_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index(
            "ix_staff_commission_rule_lookup",
            "tenant_id",
            "staff_id",
            "item_type",
            "effective_from",
        ),
    )


class PayRun(Base):
    __tablename__ = "staff_pay_runs"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    status = Column(
        String(16), nullable=False, default=PAY_RUN_DRAFT, server_default=PAY_RUN_DRAFT
    )
    total_cents = Column(Integer, nullable=False, default=0, server_default=text("0"))
    created_by_user_id = Column(Integer, nullable=True)
    finalized_by_user_id = Column(Integer, nullable=True)
    paid_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    finalized_at = Column(DateTime(timezone=True), nullable=True)
    paid_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_staff_pay_run_tenant_period", "tenant_id", "period_start", "period_end"),
    )


class CommissionEarning(Base):
    __tablename__ = "staff_commission_earnings"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    staff_id = Column(
        Integer,
        ForeignKey("booking_staff.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    order_id = Column(
        Integer, ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    order_item_id = Column(
        Integer, ForeignKey("order_items.id", ondelete="RESTRICT"), nullable=True
    )
    pay_run_id = Column(
        Integer, ForeignKey("staff_pay_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    reversal_of_id = Column(
        Integer,
        ForeignKey("staff_commission_earnings.id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
    )
    source_key = Column(String(64), nullable=False)
    item_type = Column(String(16), nullable=False)
    item_name_snapshot = Column(String(128), nullable=False)
    gross_cents = Column(Integer, nullable=False)
    net_cents = Column(Integer, nullable=False)
    calculation_basis = Column(String(16), nullable=False)
    method_snapshot = Column(String(16), nullable=False)
    value_snapshot = Column(Integer, nullable=False)
    commission_cents = Column(Integer, nullable=False)
    earned_at = Column(DateTime(timezone=True), nullable=False)
    reversed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("tenant_id", "source_key", name="uq_commission_earning_source"),
        Index("ix_commission_earning_unsettled", "tenant_id", "pay_run_id", "earned_at"),
    )


class PayRunItem(Base):
    __tablename__ = "staff_pay_run_items"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    pay_run_id = Column(
        Integer, ForeignKey("staff_pay_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    staff_id = Column(
        Integer,
        ForeignKey("booking_staff.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    commission_cents = Column(Integer, nullable=False, default=0, server_default=text("0"))
    tip_cents = Column(Integer, nullable=False, default=0, server_default=text("0"))
    adjustment_cents = Column(Integer, nullable=False, default=0, server_default=text("0"))
    total_cents = Column(Integer, nullable=False, default=0, server_default=text("0"))
    adjustment_note = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("pay_run_id", "staff_id", name="uq_staff_pay_run_item"),
    )
