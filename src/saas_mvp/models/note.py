"""Note model — the core resource for CRUD demo."""

from sqlalchemy import Column, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from saas_mvp.db import Base


class Note(Base):
    __tablename__ = "notes"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(256), nullable=False)
    content = Column(Text, nullable=False, default="")
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)

    owner = relationship("User", back_populates="notes")
    tenant = relationship("Tenant", back_populates="notes")
