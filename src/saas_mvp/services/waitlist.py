"""額滿候補（waitlist）服務 — 登記 / 查詢 / 取消 / 回補通知。

流程：
  * 顧客在 LINE 端遇到時段額滿 → `join_waitlist` 登記（僅額滿時可登記；
    重複登記走 reactivate 冪等，同一 (slot, line_user) 一列）。
  * 取消/改期回補容量時，booking 服務在**同一交易的時段鎖內**呼叫
    `pick_first_eligible_in_txn` 挑出第一位人數符合的 waiting 候補標為
    notified（防兩個並發取消通知同一人），commit 後呼叫
    `notify_candidate_best_effort` 推播「立即預約」按鈕。
  * 推播走月度推播額度（has_push_quota / consume_push_in_txn）；額度罄或
    推播失敗時該候補退回 waiting，下次回補再試。通知為 best-effort，
    **絕不影響取消/改期主流程**（比照 _publish_reservation_event）。
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.booking_waitlist import (
    WAITLIST_CANCELLED,
    WAITLIST_NOTIFIED,
    WAITLIST_WAITING,
    WaitlistEntry,
)
from saas_mvp.models.line_channel_config import LineChannelConfig
from saas_mvp.services import push_quota as push_quota_svc
from saas_mvp.services.tenants import tenant_query

_log = logging.getLogger(__name__)


class WaitlistError(Exception):
    """候補操作的預期錯誤基底。"""


class WaitlistSlotNotFound(WaitlistError):
    """時段不存在或跨租戶。"""


class SlotNotFullError(WaitlistError):
    """時段尚有名額，應直接預約而非候補。"""


class WaitlistEntryNotFound(WaitlistError):
    """候補紀錄不存在或非本人。"""


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _default_push_client():
    """通知用 push client 工廠（測試可 monkeypatch 為 FakeLinePushClient）。"""
    from saas_mvp.line_client import HttpLinePushClient

    return HttpLinePushClient()


def _available(slot: BookingSlot) -> int:
    return (
        (slot.max_capacity or 0)
        - (slot.walkin_reserved or 0)
        - (slot.booked_count or 0)
    )


def join_waitlist(
    db: Session,
    *,
    tenant_id: int,
    slot_id: int,
    line_user_id: str,
    party_size: int = 1,
    display_name: str | None = None,
) -> WaitlistEntry:
    """登記候補；僅在時段額滿（可約 < party_size）時允許。

    重複登記（同 slot + line_user）reactivate 既有列（更新人數、
    狀態回 waiting），冪等。
    """
    slot = (
        tenant_query(db, BookingSlot, tenant_id)
        .filter(BookingSlot.id == slot_id)
        .first()
    )
    if slot is None or not slot.is_active:
        raise WaitlistSlotNotFound(f"slot {slot_id} not found")
    if party_size < 1:
        party_size = 1
    if _available(slot) >= party_size:
        raise SlotNotFullError(f"slot {slot_id} still has capacity")

    entry = WaitlistEntry(
        tenant_id=tenant_id,
        slot_id=slot_id,
        line_user_id=line_user_id,
        display_name=display_name,
        party_size=party_size,
        status=WAITLIST_WAITING,
    )
    db.add(entry)
    try:
        db.commit()
        db.refresh(entry)
        return entry
    except IntegrityError:
        db.rollback()
        existing = db.execute(
            select(WaitlistEntry).where(
                WaitlistEntry.slot_id == slot_id,
                WaitlistEntry.line_user_id == line_user_id,
            )
        ).scalar_one_or_none()
        if existing is None:  # pragma: no cover - race 防禦
            raise
        existing.status = WAITLIST_WAITING
        existing.party_size = party_size
        if display_name:
            existing.display_name = display_name
        existing.notified_at = None
        existing.updated_at = _utcnow()
        db.commit()
        db.refresh(existing)
        return existing


def list_my_waitlist(
    db: Session, *, tenant_id: int, line_user_id: str
) -> list[WaitlistEntry]:
    """顧客的有效候補（waiting / notified）。"""
    return list(
        tenant_query(db, WaitlistEntry, tenant_id)
        .filter(
            WaitlistEntry.line_user_id == line_user_id,
            WaitlistEntry.status.in_([WAITLIST_WAITING, WAITLIST_NOTIFIED]),
        )
        .order_by(WaitlistEntry.id)
        .all()
    )


def cancel_waitlist(
    db: Session, *, tenant_id: int, entry_id: int, line_user_id: str
) -> WaitlistEntry:
    """顧客取消自己的候補；查無/他人拋 WaitlistEntryNotFound。冪等。"""
    entry = (
        tenant_query(db, WaitlistEntry, tenant_id)
        .filter(WaitlistEntry.id == entry_id)
        .first()
    )
    if entry is None or entry.line_user_id != line_user_id:
        raise WaitlistEntryNotFound(f"waitlist entry {entry_id} not found")
    if entry.status != WAITLIST_CANCELLED:
        entry.status = WAITLIST_CANCELLED
        entry.updated_at = _utcnow()
        db.commit()
        db.refresh(entry)
    return entry


def pick_first_eligible_in_txn(
    db: Session, *, tenant_id: int, slot: BookingSlot
) -> int | None:
    """回補交易鎖內：挑第一位人數符合的 waiting 候補標為 notified。

    **不 commit**（由呼叫端的取消/改期交易一併提交）；必須在該時段
    FOR UPDATE 鎖內呼叫，保證兩個並發回補不會通知同一人。
    回傳被選中的 entry id（commit 後交給 notify_candidate_best_effort），
    無符合者回 None。
    """
    available = _available(slot)
    if available <= 0:
        return None
    entry = db.execute(
        select(WaitlistEntry)
        .where(
            WaitlistEntry.tenant_id == tenant_id,
            WaitlistEntry.slot_id == slot.id,
            WaitlistEntry.status == WAITLIST_WAITING,
            WaitlistEntry.party_size <= available,
        )
        .order_by(WaitlistEntry.id)
        .limit(1)
    ).scalar_one_or_none()
    if entry is None:
        return None
    entry.status = WAITLIST_NOTIFIED
    entry.notified_at = _utcnow()
    entry.updated_at = _utcnow()
    return entry.id


def notify_candidate_best_effort(
    db: Session,
    *,
    tenant_id: int,
    entry_id: int,
    push_client=None,
    now: datetime.datetime | None = None,
) -> bool:
    """commit 後推播候補通知（附「立即預約」quick reply）。

    best-effort：任何失敗（無 LINE 設定、額度罄、推播錯誤）都把該候補
    退回 waiting（下次回補再試）並回 False，**絕不拋出**。
    成功時與額度計量同交易 commit，回 True。
    """
    try:
        entry = db.get(WaitlistEntry, entry_id)
        if entry is None or entry.status != WAITLIST_NOTIFIED:
            return False
        slot = db.get(BookingSlot, entry.slot_id)
        cfg = db.execute(
            select(LineChannelConfig).where(
                LineChannelConfig.tenant_id == tenant_id
            )
        ).scalar_one_or_none()
        if slot is None or cfg is None:
            _revert_to_waiting(db, entry_id)
            return False
        if not push_quota_svc.has_push_quota(db, tenant_id, now=now):
            _revert_to_waiting(db, entry_id)
            return False

        when = slot.slot_start.strftime("%m/%d %H:%M")
        text = (
            f"好消息！您候補的時段 {when} 已釋出名額，"
            f"請點選下方按鈕完成預約（{entry.party_size} 位）。"
        )
        client = push_client or _default_push_client()
        client.push(
            entry.line_user_id,
            text,
            access_token=cfg.access_token,
            quick_reply=[
                (
                    "立即預約",
                    f"action=book&slot_id={slot.id}&party={entry.party_size}",
                )
            ],
        )
        push_quota_svc.consume_push_in_txn(db, tenant_id, now=now)
        db.commit()
        return True
    except Exception:  # noqa: BLE001 — 候補通知絕不影響主流程
        _log.warning(
            "waitlist notify failed tenant=%d entry=%d", tenant_id, entry_id,
            exc_info=True,
        )
        try:
            db.rollback()
            _revert_to_waiting(db, entry_id)
        except Exception:  # noqa: BLE001 — 連退回都失敗只能放棄
            _log.exception(
                "waitlist revert failed tenant=%d entry=%d", tenant_id, entry_id
            )
        return False


def _revert_to_waiting(db: Session, entry_id: int) -> None:
    """把 notified 候補退回 waiting（下次回補再試）。"""
    entry = db.get(WaitlistEntry, entry_id)
    if entry is not None and entry.status == WAITLIST_NOTIFIED:
        entry.status = WAITLIST_WAITING
        entry.notified_at = None
        entry.updated_at = _utcnow()
        db.commit()
