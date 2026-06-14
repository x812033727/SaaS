"""Tenant model."""

from sqlalchemy import Boolean, Column, Integer, String
from sqlalchemy.orm import relationship

from saas_mvp.db import Base


class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(128), unique=True, nullable=False, index=True)
    plan = Column(String(32), nullable=False, default="free")  # "free" | "pro"
    is_active = Column(Boolean, nullable=False, default=True)

    users = relationship("User", back_populates="tenant", cascade="all, delete-orphan")
    notes = relationship("Note", back_populates="tenant", cascade="all, delete-orphan")
    line_channel_config = relationship(
        "LineChannelConfig",
        back_populates="tenant",
        uselist=False,          # 一對一
        cascade="all, delete-orphan",
    )
