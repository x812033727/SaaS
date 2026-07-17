"""Customer model — 預約顧客 CRM 檔案，每租戶 × 每 LINE 使用者一筆。

每次 LINE 預約成功時自動建立或更新（booking_count 遞增、last_booked_at 更新），
店家端可透過 /booking/customers 唯讀查詢、PATCH 補 phone/note。

唯一約束：(tenant_id, line_user_id) 一對一（仿 LineUserLanguage），upsert 更新即可。
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Boolean,
    Column,
    Date,
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
    # LINE 來源唯一鍵；NULL = 店家自建/CSV 匯入的無 LINE 顧客（推播路徑
    # 一律 guard None）。unique(tenant_id, line_user_id) 對 NULL 不設限
    #（SQLite/PG 皆允許多 NULL）。Alembic rev 0003 放寬自 NOT NULL。
    line_user_id = Column(String(64), nullable=True, index=True)
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
    # 顧客自助入口網憑證(R5-B1「我的預約」);token 即能力、長效可輪替,
    # NULL = 尚未產生(惰性簽發,比照 ics_token)。migration 0048。
    portal_token = Column(String(64), nullable=True, unique=True)
    # 顧客 email(R5-B3,選填):提醒三段 fallback(LINE→SMS→email)第三管道;
    # booking_form 選填欄位與 portal 頁自助填寫。migration 0049。
    email = Column(String(255), nullable=True)
    # 行銷退訂(R6-B1,PDPA):非 NULL = 已退訂**行銷**推播(交易性通知如
    # 建單/提醒/取消/退款不受影響、恆送);既有顧客 NULL = 訂閱中(opt-out 模型)。
    # unsubscribe_token 為退訂連結能力憑證(惰性簽發,比照 portal_token)。migration 0054。
    marketing_opt_out_at = Column(DateTime(timezone=True), nullable=True)
    unsubscribe_token = Column(String(64), nullable=True, unique=True)
    # 生日（PHASE 4 行銷自動化：生日活動 targeting）；nullable = 未填。
    # 既有 DB 由 _migrate_add_customer_birthday() 補欄。
    birthday = Column(Date, nullable=True)
    # 黑名單：True = 禁止此 LINE 顧客線上預約（book_slot 早退拒絕）；reason 選填供店家記事。
    # 既有 DB 由 _migrate_add_customer_blacklist() 補欄。
    blacklisted = Column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    blacklist_reason = Column(String(255), nullable=True)
    # LINE 好友狀態：webhook follow/unfollow 事件回寫。False = 已封鎖/解除好友，
    # 行銷推播跳過（省推播額度、免 LINE push 必然失敗）。預設 True：歷史顧客
    # 無從得知封鎖狀態，沿用「全部視為好友」的原行為。Alembic rev 0005 補欄。
    line_followed = Column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    line_followed_at = Column(DateTime(timezone=True), nullable=True)
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
        # PHASE 4-1：全新顧客建檔即觸發 welcome 行銷（僅入列 pending，不同步發送）。
        # 行為閘在 MARKETING_AUTO 之後；hook 失敗不得阻擋顧客建檔（建檔為主路徑）。
        _maybe_enqueue_welcome(db, tenant_id, row)
        return row

    if display_name and not row.display_name:
        row.display_name = display_name
    if bump_booking:
        row.booking_count = (row.booking_count or 0) + 1
        row.last_booked_at = now
    return row


def _maybe_enqueue_welcome(db: Session, tenant_id: int, customer: Customer) -> None:
    """全新顧客建檔時，若租戶開通 MARKETING_AUTO 則入列 welcome CampaignSend。

    最小化、向後相容：feature 關閉或無 welcome 活動即 no-op；任何失敗吞掉
    （顧客建檔是主路徑，行銷 hook 不得使其失敗）。lazy import 避免循環依賴。
    """
    try:
        from saas_mvp.services import features as features_svc

        if not features_svc.is_enabled(db, tenant_id, features_svc.MARKETING_AUTO):
            return
        from saas_mvp.services import marketing as marketing_svc

        marketing_svc.create_welcome_send(db, customer)
    except Exception:  # noqa: BLE001 - welcome hook must never block customer upsert
        pass
