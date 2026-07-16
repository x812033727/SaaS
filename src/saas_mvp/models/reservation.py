"""Reservation model — 單筆預約。

容量計數維護在 BookingSlot.booked_count（建單 +party_size、取消 -party_size），
本表只記預約本身的狀態。line_user_id denormalize 在此，供 LINE「我的預約」查詢與
提醒推播直接取用，免再 join customer。

狀態：confirmed / cancelled（軟取消，保留歷史）。
"""

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

# 狀態常數（避免散落字串硬碼）
RESERVATION_CONFIRMED = "confirmed"
RESERVATION_CANCELLED = "cancelled"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Reservation(Base):
    __tablename__ = "booking_reservations"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    slot_id = Column(
        Integer,
        ForeignKey("booking_slots.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # LINE 來源建單時回填；店家端手動建單可為 NULL。
    customer_id = Column(
        Integer,
        ForeignKey("booking_customers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    line_user_id = Column(String(64), nullable=True, index=True)
    party_size = Column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    status = Column(
        String(16),
        nullable=False,
        default=RESERVATION_CONFIRMED,
        server_default=RESERVATION_CONFIRMED,
    )
    note = Column(Text, nullable=True)
    # 到場與否（P5 報表算爽約率用）；NULL=未標記，既有 DB 由 _migrate_add_reservation_attended() 補欄。
    attended = Column(Boolean, nullable=True)
    # PHASE 1：指派員工 / 服務項目（皆 nullable = 未指派 / 任意）；
    # 既有 DB 由 _migrate_add_reservation_staff_id() / _migrate_add_reservation_service_id() 補欄。
    staff_id = Column(Integer, nullable=True, index=True)
    service_id = Column(Integer, nullable=True, index=True)
    # 分店綁定（多分店，nullable = 不限分店）；歷史上由 _migrate_add_location_id()
    # 對舊 DB 補欄，model 現已宣告使 metadata 成為 schema 唯一真相（Alembic baseline）。
    location_id = Column(Integer, nullable=True)
    # 顧客自助確認出席時間（提醒訊息「確認出席」按鈕）；NULL=未確認。
    # 既有 DB 由 _migrate_add_reservation_customer_confirmed() 補欄。
    customer_confirmed_at = Column(DateTime(timezone=True), nullable=True)
    # 定金（C4）:建單快照。status NULL=不需定金|pending|paid|expired。
    # trade_no unique(綠界回調對單);逾時由 ops/cancel_unpaid_deposits 取消回補。rev 0015。
    deposit_cents = Column(Integer, nullable=True)
    deposit_status = Column(String(16), nullable=True)
    deposit_merchant_trade_no = Column(String(20), nullable=True, unique=True)
    deposit_paid_at = Column(DateTime(timezone=True), nullable=True)
    deposit_expires_at = Column(DateTime(timezone=True), nullable=True)
    # 付款結果快照：退款必須使用原交易的 provider / MerchantID / TradeNo，
    # 不可只依賴之後可能被管理員更換的平台金流設定。
    deposit_provider = Column(String(16), nullable=True)
    deposit_provider_merchant_id = Column(String(64), nullable=True)
    deposit_provider_trade_no = Column(String(20), nullable=True)
    deposit_payment_type = Column(String(32), nullable=True)
    # LINE Pay 定金 Request API 回傳的 transactionId;deposit-confirm 時必須與
    # query string 比對一致(txid↔reservation 綁定)。rev 0041。
    deposit_payment_txn_id = Column(String(32), nullable=True)
    # 全額退款狀態：NULL(未申請) | processing | refunded | failed |
    # manual_required。網路結果不確定時只允許人工核對，避免重複退刷。
    deposit_refund_status = Column(String(24), nullable=True)
    deposit_refund_attempts = Column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    deposit_refund_requested_at = Column(DateTime(timezone=True), nullable=True)
    deposit_refunded_at = Column(DateTime(timezone=True), nullable=True)
    deposit_refund_error = Column(String(255), nullable=True)
    deposit_refund_provider_code = Column(String(32), nullable=True)
    deposit_refund_requested_by_user_id = Column(Integer, nullable=True)

    # E1:Google Calendar 事件 id(未連結/同步失敗為 NULL)。
    gcal_event_id = Column(String(128), nullable=True)
    # A0.2 冪等:此預約由哪筆 LINE webhook 事件建立(非 LINE 來源=NULL)。webhook
    # 重放時 book_slot 以 (tenant_id, source_webhook_event_id) 查得既有預約即回傳,
    # 不重複建單。NULL 不受唯一約束限制(多筆 NULL 合法)。
    source_webhook_event_id = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )
    cancelled_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_reservation_tenant_status", "tenant_id", "status"),
        # webhook 重放冪等:同一租戶同一 webhook 事件至多一筆預約(NULL 不受限)。
        UniqueConstraint(
            "tenant_id",
            "source_webhook_event_id",
            name="uq_reservation_source_webhook_event",
        ),
    )
