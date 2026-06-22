"""Staff model — 員工 / 服務人員（員工排班 / staff scheduling）。

每名員工可綁定一家分店（location_id，nullable = 不限分店）。
access_token 為員工自助入口（/s/{token}）的憑證：token 即能力（capability），
解析時不套租戶 filter，由 token 唯一性決定身份。

booking_mode：
  - 'capacity'   ：沿用時段容量計數（多人共用時段名額）。
  - 'one_to_one' ：一對一服務，同一時段同一員工僅能有一筆 confirmed 預約。
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
    text,
)

from saas_mvp.db import Base

# booking_mode 常數
STAFF_MODE_CAPACITY = "capacity"
STAFF_MODE_ONE_TO_ONE = "one_to_one"
VALID_STAFF_MODES = frozenset({STAFF_MODE_CAPACITY, STAFF_MODE_ONE_TO_ONE})


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Staff(Base):
    __tablename__ = "booking_staff"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 分店綁定（nullable = 不限分店）；分店刪除時設為 NULL，員工保留。
    location_id = Column(
        Integer,
        ForeignKey("booking_locations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    name = Column(String(128), nullable=False)
    role = Column(String(64), nullable=True)
    is_active = Column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    # 員工自助入口憑證；NULL = 尚未發放。unique（SQLite 允許多 NULL）。
    access_token = Column(String(64), nullable=True, unique=True)
    booking_mode = Column(
        String(16),
        nullable=False,
        default=STAFF_MODE_CAPACITY,
        server_default=STAFF_MODE_CAPACITY,
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )
