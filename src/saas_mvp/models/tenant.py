"""Tenant model."""

from sqlalchemy import Boolean, Column, Integer, String
from sqlalchemy.orm import relationship

from saas_mvp.db import Base

# 已知店家類型（建議值）；store_type 為「分類標籤 + 篩選」用途，不驅動任何 bot 行為。
# 採軟驗證：未知值仍接受（保持自由標籤），僅在 router 層用 max_length=32 擋過長。
KNOWN_STORE_TYPES: frozenset[str] = frozenset(
    {"restaurant", "retail", "service", "other"}
)


def normalize_store_type(value: str | None) -> str | None:
    """正規化 store_type：strip + lowercase；空字串/None 一律回 None（未分類）。

    刻意不對未知值報錯——store_type 是自由標籤，未知值合法保留。
    """
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized or None


class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(128), unique=True, nullable=False, index=True)
    plan = Column(String(32), nullable=False, default="free")  # "free" | "pro"
    is_active = Column(Boolean, nullable=False, default=True)
    # 店家類型（標籤 + 篩選用，無 unique）；NULL = 未分類。
    store_type = Column(String(32), nullable=True, default=None)
    # 行事曆 ICS 訂閱憑證（店家整店 feed）；token 即能力，NULL = 尚未產生。
    # 既有 DB 由 _migrate_add_tenant_ics_token() 補欄 + unique index。
    ics_token = Column(String(64), nullable=True, unique=True)

    users = relationship("User", back_populates="tenant", cascade="all, delete-orphan")
    notes = relationship("Note", back_populates="tenant", cascade="all, delete-orphan")
    line_channel_config = relationship(
        "LineChannelConfig",
        back_populates="tenant",
        uselist=False,          # 一對一
        cascade="all, delete-orphan",
    )
