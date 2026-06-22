"""StaffShift model — 員工固定班表（每週幾、起訖時間、輪值）。

weekday：0=週一 … 6=週日（Python datetime.weekday() 慣例），nullable = 不分平日。
start_time / end_time：'HH:MM' 字串（與時段日期解耦，純比時刻）。
rotation：'day' | 'night' | 'off'（off 班表存在但不視為可服務時段）。

唯一約束：(staff_id, weekday, start_time) 避免同員工同日同起時重複建班。
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    text,
)

from saas_mvp.db import Base


class StaffShift(Base):
    __tablename__ = "booking_staff_shifts"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    staff_id = Column(
        Integer,
        ForeignKey("booking_staff.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    weekday = Column(Integer, nullable=True)  # 0-6（None = 不分平日）
    start_time = Column(String(5), nullable=False)  # 'HH:MM'
    end_time = Column(String(5), nullable=False)  # 'HH:MM'
    rotation = Column(String(8), nullable=True)  # 'day' | 'night' | 'off'
    is_active = Column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )

    __table_args__ = (
        UniqueConstraint(
            "staff_id", "weekday", "start_time", name="uq_staff_shift"
        ),
    )
