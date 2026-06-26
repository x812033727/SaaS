"""預約提醒服務 — 入列、取消、訊息組裝。

派送（掃描 due → 推播）由 ops 腳本 saas_mvp.ops.send_due_reminders 執行，
本模組只負責「建單時入列兩筆提醒」「取消時標 skipped」與「組裝提醒文字」。

入列不 commit：與 booking.create_reservation 同一交易內一起 commit，確保
「預約成功 ⇔ 提醒已排程」原子一致。
"""

from __future__ import annotations

import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.reservation import Reservation
from saas_mvp.models.reservation_reminder import (
    REMINDER_DAY_BEFORE,
    REMINDER_DAY_OF,
    REMINDER_PENDING,
    REMINDER_SKIPPED,
    ReservationReminder,
)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def compute_remind_times(
    slot_start: datetime.datetime,
    day_of_lead_minutes: int,
    hours_before: int = 24,
) -> dict[str, datetime.datetime]:
    """由 slot_start 算出兩種提醒的 remind_at（offset 式，易於測試）。

    ``hours_before``：「預約前提醒」提前的小時數（對標 vibeaico「自訂提醒時間（小時）」）。
    預設 24（＝前一天）；店家可自訂為任意正整數小時。
    """
    return {
        REMINDER_DAY_BEFORE: slot_start - datetime.timedelta(hours=hours_before),
        REMINDER_DAY_OF: slot_start
        - datetime.timedelta(minutes=day_of_lead_minutes),
    }


def enqueue_reminders(
    db: Session,
    *,
    reservation: Reservation,
    slot: BookingSlot,
    day_of_lead_minutes: int,
    hours_before: int = 24,
    enabled: bool = True,
) -> int:
    """為一筆預約入列 day_before / day_of 兩筆 pending 提醒（不 commit）。

    僅在 enabled 且 reservation 有 line_user_id（可推播）時入列。
    重複入列由 UniqueConstraint(reservation_id, kind) 擋下（catch IntegrityError 跳過）。
    回傳實際新增筆數。
    """
    if not enabled or not reservation.line_user_id:
        return 0

    times = compute_remind_times(
        slot.slot_start, day_of_lead_minutes, hours_before=hours_before
    )
    added = 0
    for kind, remind_at in times.items():
        row = ReservationReminder(
            tenant_id=reservation.tenant_id,
            reservation_id=reservation.id,
            line_user_id=reservation.line_user_id,
            kind=kind,
            remind_at=remind_at,
            status=REMINDER_PENDING,
        )
        db.add(row)
        try:
            db.flush()  # 觸發 unique 約束；重複則 IntegrityError
            added += 1
        except IntegrityError:
            db.rollback()  # 回滾此筆 flush，繼續其他 kind
    return added


def cancel_reminders_for_reservation(db: Session, *, reservation_id: int) -> int:
    """把某預約所有 pending 提醒標為 skipped（取消預約時呼叫，不 commit）。

    回傳受影響筆數。
    """
    result = db.execute(
        update(ReservationReminder)
        .where(
            ReservationReminder.reservation_id == reservation_id,
            ReservationReminder.status == REMINDER_PENDING,
        )
        .values(status=REMINDER_SKIPPED, updated_at=_utcnow())
    )
    return result.rowcount or 0


def list_due_reminders(
    db: Session, *, now: datetime.datetime, limit: int
) -> list[ReservationReminder]:
    """讀取已到期（remind_at <= now）且仍 pending 的提醒（供 ops 腳本掃描）。"""
    return list(
        db.execute(
            select(ReservationReminder)
            .where(
                ReservationReminder.status == REMINDER_PENDING,
                ReservationReminder.remind_at <= now,
            )
            .order_by(ReservationReminder.remind_at)
            .limit(limit)
        ).scalars()
    )


def build_reminder_text(
    *, slot: BookingSlot, reservation: Reservation, store_name: str
) -> str:
    """組裝提醒訊息（含時間、人數、店名、取消方式）。"""
    when = slot.slot_start.strftime("%Y-%m-%d %H:%M")
    return (
        f"【預約提醒】{store_name}\n"
        f"時間：{when}\n"
        f"人數：{reservation.party_size} 位\n"
        f"預約編號：{reservation.id}\n"
        f"如需取消請回覆：/cancel {reservation.id}"
    )
