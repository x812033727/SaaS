"""預約後滿意度調查服務（A3.3）。

* ``list_due_requests``：撈「服務已結束、confirmed、有 line_user_id、尚未發過
  問卷、租戶開通 FEEDBACK_SURVEY」的預約（供 cron 派發）。
* ``mark_requested``：發問卷同交易入列（reservation_id unique 天然冪等）。
* ``record_score``：webhook `rate` action 寫分數（重複點分數 = 更新，不重複建列）。
* ``summary``：均分/回覆率（供報表）。
"""

from __future__ import annotations

import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.reservation import RESERVATION_CONFIRMED, Reservation
from saas_mvp.models.reservation_feedback import ReservationFeedback
from saas_mvp.services import features as features_svc


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def list_due_requests(
    db: Session, *, now: datetime.datetime | None = None, limit: int = 200
) -> list[tuple[Reservation, BookingSlot]]:
    """服務已結束（slot_end 過；無 slot_end 用 slot_start + 1h）且未發問卷的預約。

    feature 閘門在本函式內逐租戶檢查（cron 跨租戶掃描）。
    """
    effective_now = now or _utcnow()
    rows = db.execute(
        select(Reservation, BookingSlot)
        .join(BookingSlot, Reservation.slot_id == BookingSlot.id)
        .outerjoin(
            ReservationFeedback,
            ReservationFeedback.reservation_id == Reservation.id,
        )
        .where(
            Reservation.status == RESERVATION_CONFIRMED,
            Reservation.line_user_id.is_not(None),
            ReservationFeedback.id.is_(None),
        )
        .order_by(Reservation.id)
        .limit(limit)
    ).all()

    due: list[tuple[Reservation, BookingSlot]] = []
    naive_now = effective_now.replace(tzinfo=None)
    for resv, slot in rows:
        end = slot.slot_end or (
            slot.slot_start + datetime.timedelta(hours=1) if slot.slot_start else None
        )
        if end is None:
            continue
        cmp_now = naive_now if end.tzinfo is None else effective_now
        if end > cmp_now:
            continue
        if not features_svc.is_enabled(
            db, resv.tenant_id, features_svc.FEEDBACK_SURVEY
        ):
            continue
        due.append((resv, slot))
    return due


def mark_requested(db: Session, resv: Reservation) -> ReservationFeedback:
    """入列問卷請求（不 commit，由呼叫端與推播成敗一起提交）。"""
    row = ReservationFeedback(
        tenant_id=resv.tenant_id,
        reservation_id=resv.id,
        line_user_id=resv.line_user_id,
        requested_at=_utcnow(),
    )
    db.add(row)
    return row


def record_score(
    db: Session, *, tenant_id: int, reservation_id: int, line_user_id: str, score: int
) -> ReservationFeedback | None:
    """寫回分數（1–5）；驗擁有者。查無問卷列（未發卷就點？）回 None。commit。"""
    if not 1 <= score <= 5:
        return None
    row = db.execute(
        select(ReservationFeedback).where(
            ReservationFeedback.tenant_id == tenant_id,
            ReservationFeedback.reservation_id == reservation_id,
            ReservationFeedback.line_user_id == line_user_id,
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    row.score = score
    row.responded_at = _utcnow()
    db.commit()
    return row


def summary(db: Session, tenant_id: int) -> dict:
    """均分 + 回覆率（供報表/儀表板）。"""
    total, responded, avg_score = db.execute(
        select(
            func.count(ReservationFeedback.id),
            func.count(ReservationFeedback.score),
            func.avg(ReservationFeedback.score),
        ).where(ReservationFeedback.tenant_id == tenant_id)
    ).one()
    return {
        "requested": int(total or 0),
        "responded": int(responded or 0),
        "response_rate": round(responded / total, 2) if total else 0.0,
        "avg_score": round(float(avg_score), 2) if avg_score is not None else None,
    }
