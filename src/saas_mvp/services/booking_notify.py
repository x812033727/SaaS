"""預約異動通知服務 — 入列、文字組裝、標記。

店家在後台修改（reschedule）或取消預約時呼叫 enqueue_change / enqueue_cancel，
在**同一交易內** INSERT 一筆 BookingNotification（不 commit，由呼叫端一起 commit），
確保「異動成功 ⇔ 通知已排程」原子一致（比照 reminders.enqueue_reminders）。

派送（掃描 due → 推播）由 ops 腳本 saas_mvp.ops.send_due_notifications 執行。

入列閘門：BOOKING_NOTIFY 功能旗標開通 且 reservation 有 line_user_id（可推播）。
冪等：UniqueConstraint(reservation_id, kind) 擋重複入列（catch IntegrityError）。
"""

from __future__ import annotations

import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from saas_mvp.models.booking_notification import (
    NOTIFY_CANCEL,
    NOTIFY_CHANGE,
    NOTIFY_FAILED,
    NOTIFY_PENDING,
    NOTIFY_REFUND,
    NOTIFY_SENT,
    BookingNotification,
)
from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.reservation import Reservation


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# ── 文字組裝（繁體中文，友善語氣） ────────────────────────────────────────────

def build_change_text(
    reservation: Reservation,
    slot: BookingSlot,
    old_slot: BookingSlot | None = None,
) -> str:
    """組裝「預約已更改」通知文字。"""
    when = slot.slot_start.strftime("%Y-%m-%d %H:%M")
    lines = [
        "【預約異動通知】",
        f"您的預約（編號 {reservation.id}）已更新。",
    ]
    if old_slot is not None:
        old_when = old_slot.slot_start.strftime("%Y-%m-%d %H:%M")
        lines.append(f"原時間：{old_when}")
    lines.append(f"新時間：{when}")
    lines.append(f"人數：{reservation.party_size} 位")
    lines.append("如有疑問，歡迎與我們聯繫，謝謝您！")
    return "\n".join(lines)


def build_cancel_text(reservation: Reservation, slot: BookingSlot) -> str:
    """組裝「預約已取消」通知文字。"""
    when = slot.slot_start.strftime("%Y-%m-%d %H:%M")
    return (
        "【預約取消通知】\n"
        f"您原訂於 {when} 的預約（編號 {reservation.id}）已被取消。\n"
        "如需重新預約，歡迎再次與我們聯繫，謝謝您！"
    )


# ── 入列（同交易、不 commit、冪等） ──────────────────────────────────────────

def _enqueue(
    db: Session,
    *,
    reservation: Reservation,
    kind: str,
    payload_text: str,
    enabled: bool,
    send_after: datetime.datetime | None = None,
) -> int:
    """共用入列邏輯；回傳實際新增筆數（0 或 1）。"""
    if not enabled or not reservation.line_user_id:
        return 0
    row = BookingNotification(
        tenant_id=reservation.tenant_id,
        reservation_id=reservation.id,
        line_user_id=reservation.line_user_id,
        kind=kind,
        status=NOTIFY_PENDING,
        payload_text=payload_text,
        send_after=send_after or _utcnow(),
    )
    db.add(row)
    try:
        db.flush()  # 觸發 unique 約束；重複則 IntegrityError
    except IntegrityError:
        db.rollback()  # 回滾此筆 flush（同預約同 kind 已存在）
        return 0
    return 1


def enqueue_change(
    db: Session,
    *,
    reservation: Reservation,
    slot: BookingSlot,
    old_slot: BookingSlot | None = None,
    enabled: bool = True,
    send_after: datetime.datetime | None = None,
) -> int:
    """為一筆異動入列 change 通知（不 commit）。"""
    text = build_change_text(reservation, slot, old_slot)
    return _enqueue(
        db,
        reservation=reservation,
        kind=NOTIFY_CHANGE,
        payload_text=text,
        enabled=enabled,
        send_after=send_after,
    )


def enqueue_cancel(
    db: Session,
    *,
    reservation: Reservation,
    slot: BookingSlot,
    enabled: bool = True,
    send_after: datetime.datetime | None = None,
) -> int:
    """為一筆取消入列 cancel 通知（不 commit）。"""
    text = build_cancel_text(reservation, slot)
    return _enqueue(
        db,
        reservation=reservation,
        kind=NOTIFY_CANCEL,
        payload_text=text,
        enabled=enabled,
        send_after=send_after,
    )


def build_refund_text(reservation: Reservation, amount_cents: int) -> str:
    """組裝「定金已退款」通知文字。"""
    amount = (amount_cents or 0) // 100
    total = (reservation.deposit_cents or 0) // 100
    if amount_cents == reservation.deposit_cents:
        line = f"您預約(編號 {reservation.id})的定金 NT${total} 已全額退款。"
    else:
        line = f"您預約(編號 {reservation.id})的定金 NT${total} 已部分退款 NT${amount}。"
    return (
        "【定金退款通知】\n"
        f"{line}\n"
        "退款入帳時間依發卡行/金流業者作業為準。如有疑問,歡迎與我們聯繫,謝謝您!"
    )


def enqueue_refund(
    db: Session,
    *,
    reservation: Reservation,
    amount_cents: int,
    enabled: bool = True,
    send_after: datetime.datetime | None = None,
) -> int:
    """為一筆定金退款入列通知(不 commit)。

    冪等同 change/cancel:UniqueConstraint(reservation_id, kind);退款本身
    一次即終態,重複入列不可能成功第二次。
    """
    text = build_refund_text(reservation, amount_cents)
    return _enqueue(
        db,
        reservation=reservation,
        kind=NOTIFY_REFUND,
        payload_text=text,
        enabled=enabled,
        send_after=send_after,
    )


# ── 掃描 / 標記（供 ops 腳本） ────────────────────────────────────────────────

def list_due_notifications(
    db: Session, *, now: datetime.datetime, limit: int
) -> list[BookingNotification]:
    """讀取已到期（send_after <= now）且仍 pending 的通知。"""
    return list(
        db.execute(
            select(BookingNotification)
            .where(
                BookingNotification.status == NOTIFY_PENDING,
                BookingNotification.send_after <= now,
            )
            .order_by(BookingNotification.send_after)
            .limit(limit)
        ).scalars()
    )


def mark_sent(
    notification: BookingNotification, *, now: datetime.datetime
) -> None:
    notification.status = NOTIFY_SENT
    notification.sent_at = now
    notification.attempt_count = (notification.attempt_count or 0) + 1
    notification.updated_at = now


def mark_failed(
    notification: BookingNotification, *, now: datetime.datetime, error: str
) -> None:
    notification.status = NOTIFY_FAILED
    notification.attempt_count = (notification.attempt_count or 0) + 1
    notification.last_error = error[:255]
    notification.updated_at = now
