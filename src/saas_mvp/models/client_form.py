"""預約諮詢表／同意書：可編輯範本與不可變的預約簽署快照。"""

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


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class ClientFormTemplate(Base):
    __tablename__ = "client_form_templates"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    service_id = Column(
        Integer,
        ForeignKey("booking_services.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    name = Column(String(128), nullable=False)
    intro = Column(Text, nullable=True)
    consent_text = Column(Text, nullable=False)
    require_signature = Column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    version = Column(Integer, nullable=False, default=1, server_default=text("1"))
    is_active = Column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_client_form_template_name"),
    )


class ClientFormQuestion(Base):
    __tablename__ = "client_form_questions"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    template_id = Column(
        Integer,
        ForeignKey("client_form_templates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    label = Column(String(255), nullable=False)
    field_type = Column(String(16), nullable=False)
    is_required = Column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    options_json = Column(Text, nullable=True)
    sort_order = Column(Integer, nullable=False, default=0, server_default=text("0"))
    __table_args__ = (
        Index(
            "ix_client_form_question_order", "tenant_id", "template_id", "sort_order"
        ),
    )


class ClientFormRequest(Base):
    __tablename__ = "client_form_requests"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    template_id = Column(
        Integer,
        ForeignKey("client_form_templates.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    reservation_id = Column(
        Integer,
        ForeignKey("booking_reservations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    customer_id = Column(
        Integer,
        ForeignKey("booking_customers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    token = Column(String(64), nullable=False, unique=True)
    status = Column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    template_name_snapshot = Column(String(128), nullable=False)
    intro_snapshot = Column(Text, nullable=True)
    consent_text_snapshot = Column(Text, nullable=False)
    questions_json = Column(Text, nullable=False)
    template_version = Column(Integer, nullable=False)
    require_signature_snapshot = Column(Boolean, nullable=False)
    answers_json = Column(Text, nullable=True)
    signer_name = Column(String(128), nullable=True)
    signed_at = Column(DateTime(timezone=True), nullable=True)
    submitted_ip = Column(String(64), nullable=True)
    submitted_user_agent = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "template_id",
            "reservation_id",
            name="uq_client_form_request_reservation",
        ),
        Index(
            "ix_client_form_request_customer_status",
            "tenant_id",
            "customer_id",
            "status",
        ),
    )
