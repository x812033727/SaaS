"""Customer model — 預約顧客 CRM 檔案，每租戶 × 每 LINE 使用者一筆。

每次 LINE 預約成功時自動建立或更新（booking_count 遞增、last_booked_at 更新），
店家端可透過 /booking/customers 唯讀查詢、PATCH 補 phone/note。

唯一約束：(tenant_id, line_user_id) 一對一（仿 LineUserLanguage），upsert 更新即可。
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Session

from saas_mvp.db import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Customer(Base):
    __tablename__ = "booking_customers"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # LINE 來源唯一鍵；店家手動建檔（無 line 來源）時暫不支援，故 nullable=False。
    line_user_id = Column(String(64), nullable=False, index=True)
    display_name = Column(String(128), nullable=True)
    phone = Column(String(32), nullable=True)
    booking_count = Column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_booked_at = Column(DateTime(timezone=True), nullable=True)
    note = Column(Text, nullable=True)
    # 分店綁定（PHASE 1，nullable = 不限分店）；既有 DB 由 _migrate_add_location_id() 補欄。
    location_id = Column(Integer, nullable=True, index=True)
    # 會員集點/等級（P3）；既有 DB 由 _migrate_add_customer_membership() 補欄。
    points_balance = Column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    tier = Column(
        String(16), nullable=False, default="regular", server_default="regular"
    )
    # 行事曆 ICS 訂閱憑證（顧客個人 feed）；token 即能力，NULL = 尚未產生。
    # 既有 DB 由 _migrate_add_customer_ics_token() 補欄 + unique index。
    ics_token = Column(String(64), nullable=True, unique=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "line_user_id", name="uq_booking_customer"),
    )


# ── 服務函式（供 booking service 直接呼叫） ────────────────────────────────────

def upsert_customer_from_line(
    db: Session,
    *,
    tenant_id: int,
    line_user_id: str,
    display_name: str | None = None,
    bump_booking: bool = True,
) -> Customer:
    """建立或更新 LINE 來源顧客檔；預約成功時 bump_booking=True 遞增計數。

    **不 commit**：呼叫端（booking.create_reservation）在同一交易內連同
    slot 容量遞增、reservation INSERT 一次 commit，確保原子性。
    """
    from sqlalchemy import select  # 避免頂層循環 import

    row = db.execute(
        select(Customer).where(
            Customer.tenant_id == tenant_id,
            Customer.line_user_id == line_user_id,
        )
    ).scalar_one_or_none()

    now = _utcnow()
    if row is None:
        row = Customer(
            tenant_id=tenant_id,
            line_user_id=line_user_id,
            display_name=display_name,
            booking_count=1 if bump_booking else 0,
            last_booked_at=now if bump_booking else None,
        )
        db.add(row)
        db.flush()  # 取得 row.id 供 reservation FK 回填
        return row

    if display_name and not row.display_name:
        row.display_name = display_name
    if bump_booking:
        row.booking_count = (row.booking_count or 0) + 1
        row.last_booked_at = now
    return row
