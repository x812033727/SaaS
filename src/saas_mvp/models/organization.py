"""Organization and scoped membership models.

Organization is the commercial/account boundary above tenants (brands).  The
existing ``User.tenant_id`` remains the user's primary tenant for backwards
compatibility; memberships are the source of truth for the new versioned API.
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import relationship

from saas_mvp.db import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Organization(Base):
    __tablename__ = "organizations"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(128), nullable=False)
    slug = Column(String(64), nullable=False, unique=True, index=True)
    is_active = Column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    share_customers = Column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    share_loyalty = Column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    share_coupons = Column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    tenants = relationship("Tenant", back_populates="organization")
    members = relationship(
        "OrganizationMember",
        back_populates="organization",
        cascade="all, delete-orphan",
    )


class OrganizationMember(Base):
    __tablename__ = "organization_members"

    id = Column(Integer, primary_key=True)
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role = Column(String(24), nullable=False, default="viewer", server_default="viewer")
    is_active = Column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    organization = relationship("Organization", back_populates="members")
    user = relationship("User", back_populates="organization_memberships")

    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_org_member_user"),
    )


class TenantMember(Base):
    __tablename__ = "tenant_members"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role = Column(String(24), nullable=False, default="viewer", server_default="viewer")
    is_active = Column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    tenant = relationship("Tenant", back_populates="members")
    user = relationship("User", back_populates="tenant_memberships")

    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_tenant_member_user"),
    )


class LocationMember(Base):
    __tablename__ = "location_members"

    id = Column(Integer, primary_key=True)
    location_id = Column(
        Integer,
        ForeignKey("booking_locations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role = Column(String(24), nullable=False, default="viewer", server_default="viewer")
    is_active = Column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    user = relationship("User", back_populates="location_memberships")

    __table_args__ = (
        UniqueConstraint("location_id", "user_id", name="uq_location_member_user"),
    )
