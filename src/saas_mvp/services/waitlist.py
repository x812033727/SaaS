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

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.booking_waitlist import (
    WAITLIST_BOOKED,
    WAITLIST_CANCELLED,
    WAITLIST_EXPIRED,
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


def _naive(dt: datetime.datetime) -> datetime.datetime:
    """去掉 tzinfo(統一以 UTC 語意比較;sqlite 存 naive、pg 存 aware)。"""
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _default_push_client():
    """通知用 push client 工廠（測試可 monkeypatch 為 FakeLinePushClient）。"""
    from saas_mvp.line_client import HttpLinePushClient

    return HttpLinePushClient()


def _available(slot: BookingSlot) -> int:
    # R4-B1:扣掉已為候補者保留的名額(held_count),避免對已保留量再次發 offer。
    return (
        (slot.max_capacity or 0)
        - (slot.walkin_reserved or 0)
        - (slot.booked_count or 0)
        - (slot.held_count or 0)
    )


def _release_hold(slot: BookingSlot, entry: WaitlistEntry) -> None:
    """釋放某候補 entry 的容量保留(R4-B1)。

    冪等:hold_party_size 為 NULL 時視為已釋放,不重複扣。呼叫端須已鎖 slot。
    """
    held = entry.hold_party_size
    if held is None:
        return
    slot.held_count = max(0, (slot.held_count or 0) - held)
    entry.hold_party_size = None


def _lock_slot(db: Session, slot_id: int) -> BookingSlot | None:
    return db.execute(
        select(BookingSlot).where(BookingSlot.id == slot_id).with_for_update()
    ).scalar_one_or_none()


def _offer_minutes(db: Session, tenant_id: int) -> int:
    from saas_mvp.config import settings
    from saas_mvp.models.tenant import Tenant

    tenant = db.get(Tenant, tenant_id)
    configured = tenant.waitlist_offer_minutes if tenant is not None else None
    return max(5, min(120, configured or settings.waitlist_offer_minutes_default))


def join_waitlist(
    db: Session,
    *,
    tenant_id: int,
    slot_id: int,
    line_user_id: str,
    party_size: int = 1,
    display_name: str | None = None,
    service_id: int | None = None,
    staff_id: int | None = None,
) -> WaitlistEntry:
    """登記候補；僅在時段額滿（可約 < party_size）時允許。

    重複登記（同 slot + line_user）reactivate 既有列（更新人數、
    狀態回 waiting），冪等。
    """
    slot = db.execute(
        select(BookingSlot)
        .where(BookingSlot.tenant_id == tenant_id, BookingSlot.id == slot_id)
        .with_for_update()
    ).scalar_one_or_none()
    if slot is None or not slot.is_active:
        raise WaitlistSlotNotFound(f"slot {slot_id} not found")
    if party_size < 1:
        party_size = 1
    if _available(slot) >= party_size:
        raise SlotNotFullError(f"slot {slot_id} still has capacity")

    # 保留原預約條件，但不接受跨租戶偽造的服務／員工 ID。
    if service_id is not None:
        from saas_mvp.models.service import Service

        if tenant_query(db, Service, tenant_id).filter(Service.id == service_id).first() is None:
            raise WaitlistSlotNotFound("service not found")
    if staff_id is not None:
        from saas_mvp.models.staff import Staff

        if tenant_query(db, Staff, tenant_id).filter(Staff.id == staff_id).first() is None:
            raise WaitlistSlotNotFound("staff not found")

    entry = WaitlistEntry(
        tenant_id=tenant_id,
        slot_id=slot_id,
        line_user_id=line_user_id,
        display_name=display_name,
        party_size=party_size,
        status=WAITLIST_WAITING,
        service_id=service_id,
        staff_id=staff_id,
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
        was_waiting = existing.status == WAITLIST_WAITING
        existing.status = WAITLIST_WAITING
        existing.party_size = party_size
        existing.service_id = service_id
        existing.staff_id = staff_id
        if display_name:
            existing.display_name = display_name
        existing.notified_at = None
        existing.offer_expires_at = None
        existing.reservation_id = None
        # 已取消／逾時／已完成後重新候補應排到隊尾；
        # 重複點擊「候補中」則保留原順位。
        if not was_waiting:
            existing.created_at = _utcnow()
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
    should_offer_next = entry.status in (WAITLIST_WAITING, WAITLIST_NOTIFIED)
    slot_id = entry.slot_id
    if entry.status != WAITLIST_CANCELLED:
        # R4-B1:取消 notified 候補要釋放它保留的容量。鎖 slot 後改。
        slot = _lock_slot(db, slot_id)
        if slot is not None:
            _release_hold(slot, entry)
        entry.status = WAITLIST_CANCELLED
        entry.offer_expires_at = None
        entry.updated_at = _utcnow()
        db.commit()
        db.refresh(entry)
    if should_offer_next:
        notify_next_for_slot_best_effort(
            db, tenant_id=tenant_id, slot_id=slot_id
        )
    return entry


def pick_first_eligible_in_txn(
    db: Session,
    *,
    tenant_id: int,
    slot: BookingSlot,
    now: datetime.datetime | None = None,
) -> int | None:
    """回補交易鎖內：挑第一位人數符合的 waiting 候補標為 notified。

    **不 commit**（由呼叫端的取消/改期交易一併提交）；必須在該時段
    FOR UPDATE 鎖內呼叫，保證兩個並發回補不會通知同一人。
    回傳被選中的 entry id（commit 後交給 notify_candidate_best_effort），
    無符合者回 None。
    """
    effective_now = now or _utcnow()
    # 舊版已通知列沒有期限，升級後視為已逾時；同時在鎖內收斂到期 offer，即使
    # scheduler 還沒掃到也不會卡住遞補。R4-B1:改逐筆(原為 bulk UPDATE),
    # 每筆到期 offer 都要釋放它保留的容量(_release_hold)。
    notified_here = db.execute(
        select(WaitlistEntry).where(
            WaitlistEntry.tenant_id == tenant_id,
            WaitlistEntry.slot_id == slot.id,
            WaitlistEntry.status == WAITLIST_NOTIFIED,
        )
    ).scalars().all()
    expired_any = False
    for candidate in notified_here:
        exp = candidate.offer_expires_at
        # tz-safe 比較:offer_expires_at 落庫可能 naive(sqlite)或 aware(pg),
        # effective_now 同理;統一去掉 tzinfo 後比。None=舊資料無期限,視為逾時。
        if exp is None or _naive(exp) <= _naive(effective_now):
            _release_hold(slot, candidate)
            candidate.status = WAITLIST_EXPIRED
            candidate.updated_at = effective_now
            expired_any = True
    # 讓下面的 active_offer / available 查詢看得到剛標 EXPIRED 的狀態變更 ——
    # 測試 session 為 autoflush=False,不 flush 的話 SQL 仍讀到舊 NOTIFIED
    # (原 bulk UPDATE 直接寫 DB 無此問題)。
    if expired_any:
        db.flush()
    # 同一時段一次只給一位候補者有效的先到先得窗口。
    active_offer = db.execute(
        select(WaitlistEntry.id).where(
            WaitlistEntry.tenant_id == tenant_id,
            WaitlistEntry.slot_id == slot.id,
            WaitlistEntry.status == WAITLIST_NOTIFIED,
        ).limit(1)
    ).scalar_one_or_none()
    if active_offer is not None:
        return None

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
        .order_by(WaitlistEntry.created_at, WaitlistEntry.id)
        .limit(1)
    ).scalar_one_or_none()
    if entry is None:
        return None
    entry.status = WAITLIST_NOTIFIED
    entry.notified_at = effective_now
    entry.offer_expires_at = effective_now + datetime.timedelta(
        minutes=_offer_minutes(db, tenant_id)
    )
    entry.notification_attempts = (entry.notification_attempts or 0) + 1
    entry.updated_at = effective_now
    # R4-B1:為此候補保留容量(限時內);held_count 累加,hold_party_size 兼冪等旗標。
    slot.held_count = (slot.held_count or 0) + entry.party_size
    entry.hold_party_size = entry.party_size
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
        expires = entry.offer_expires_at
        expires_text = expires.strftime("%H:%M") if expires is not None else "限時內"
        text = (
            f"好消息！您候補的時段 {when} 已釋出名額，"
            f"請在 {expires_text} 前點選下方按鈕完成預約"
            f"（{entry.party_size} 位）。名額已為您保留至此,請盡快完成預約。"
        )
        data = (
            f"action=book&slot_id={slot.id}&party={entry.party_size}"
            f"&waitlist_entry_id={entry.id}"
        )
        if entry.service_id is not None:
            data += f"&service_id={entry.service_id}"
        if entry.staff_id is not None:
            data += f"&staff_id={entry.staff_id}"
        client = push_client or _default_push_client()
        client.push(
            entry.line_user_id,
            text,
            access_token=cfg.access_token,
            quick_reply=[
                ("立即預約", data)
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
    """把 notified 候補退回 waiting（下次回補再試）。

    R4-B1:退回時釋放它保留的容量(推播失敗不該繼續佔名額)。鎖 slot 後改。
    """
    entry = db.get(WaitlistEntry, entry_id)
    if entry is not None and entry.status == WAITLIST_NOTIFIED:
        slot = _lock_slot(db, entry.slot_id)
        if slot is not None:
            _release_hold(slot, entry)
        entry.status = WAITLIST_WAITING
        entry.notified_at = None
        entry.offer_expires_at = None
        entry.updated_at = _utcnow()
        db.commit()


def own_hold_for(
    db: Session, *, tenant_id: int, slot_id: int, line_user_id: str
) -> int:
    """該顧客身為 offeree 在此時段保留的名額(R4-B1;book_slot 容量特例用)。

    只計 notified 且 hold_party_size 非空的自己的候補。呼叫端已鎖 slot。
    """
    if not line_user_id:
        return 0
    total = db.execute(
        select(WaitlistEntry.hold_party_size).where(
            WaitlistEntry.tenant_id == tenant_id,
            WaitlistEntry.slot_id == slot_id,
            WaitlistEntry.line_user_id == line_user_id,
            WaitlistEntry.status == WAITLIST_NOTIFIED,
            WaitlistEntry.hold_party_size.is_not(None),
        )
    ).scalars().all()
    return sum(int(h) for h in total if h)


def fulfill_for_booking_in_txn(
    db: Session, *, reservation, slot: BookingSlot
) -> WaitlistEntry | None:
    """建單交易內結案同一 LINE 顧客在該時段的候補。"""
    if not reservation.line_user_id:
        return None
    entry = db.execute(
        select(WaitlistEntry)
        .where(
            WaitlistEntry.tenant_id == reservation.tenant_id,
            WaitlistEntry.slot_id == slot.id,
            WaitlistEntry.line_user_id == reservation.line_user_id,
            WaitlistEntry.status.in_((WAITLIST_WAITING, WAITLIST_NOTIFIED)),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if entry is None:
        return None
    # R4-B1:offeree 建單成功 → 釋放它保留的容量(該名額已被自己的預約佔用,
    # booked_count 已加;不釋放會雙重佔用)。slot 已由 book_slot 鎖定。
    _release_hold(slot, entry)
    entry.status = WAITLIST_BOOKED
    entry.reservation_id = reservation.id
    entry.offer_expires_at = None
    entry.updated_at = _utcnow()
    return entry


def notify_next_for_slot_best_effort(
    db: Session,
    *,
    tenant_id: int,
    slot_id: int,
    push_client=None,
    now: datetime.datetime | None = None,
) -> bool:
    """鎖定時段後遞補一位；供加開容量與 scheduler 共用。"""
    try:
        slot = db.execute(
            select(BookingSlot)
            .where(
                BookingSlot.id == slot_id,
                BookingSlot.tenant_id == tenant_id,
                BookingSlot.is_active.is_(True),
            )
            .with_for_update()
        ).scalar_one_or_none()
        if slot is None:
            db.rollback()
            return False
        entry_id = pick_first_eligible_in_txn(
            db, tenant_id=tenant_id, slot=slot, now=now
        )
        db.commit()
        if entry_id is None:
            return False
        return notify_candidate_best_effort(
            db,
            tenant_id=tenant_id,
            entry_id=entry_id,
            push_client=push_client,
            now=now,
        )
    except Exception:  # noqa: BLE001 — 後台補送不可影響主流程
        db.rollback()
        _log.exception("waitlist offer failed tenant=%d slot=%d", tenant_id, slot_id)
        return False


def candidate_slots(
    db: Session, *, now: datetime.datetime | None = None, limit: int = 200
) -> list[tuple[int, int]]:
    """有等候者或到期 offer 的 (tenant_id, slot_id)，供排程收敛。"""
    effective_now = now or _utcnow()
    rows = db.execute(
        select(WaitlistEntry.tenant_id, WaitlistEntry.slot_id)
        .join(BookingSlot, BookingSlot.id == WaitlistEntry.slot_id)
        .where(
            BookingSlot.is_active.is_(True),
            BookingSlot.slot_start > effective_now,
            or_(
                WaitlistEntry.status == WAITLIST_WAITING,
                (
                    (WaitlistEntry.status == WAITLIST_NOTIFIED)
                    & or_(
                        WaitlistEntry.offer_expires_at.is_(None),
                        WaitlistEntry.offer_expires_at <= effective_now,
                    )
                ),
            ),
        )
        .distinct()
        .order_by(WaitlistEntry.slot_id)
        .limit(limit)
    ).all()
    return [(int(row[0]), int(row[1])) for row in rows]


def list_waitlist(db: Session, *, tenant_id: int) -> list[WaitlistEntry]:
    """店家後台候補清單（新到舊）。"""
    return list(
        tenant_query(db, WaitlistEntry, tenant_id)
        .order_by(WaitlistEntry.created_at.desc(), WaitlistEntry.id.desc())
        .all()
    )


def cancel_waitlist_by_staff(
    db: Session, *, tenant_id: int, entry_id: int
) -> WaitlistEntry:
    entry = (
        tenant_query(db, WaitlistEntry, tenant_id)
        .filter(WaitlistEntry.id == entry_id)
        .first()
    )
    if entry is None:
        raise WaitlistEntryNotFound(f"waitlist entry {entry_id} not found")
    if entry.status in (WAITLIST_WAITING, WAITLIST_NOTIFIED):
        slot_id = entry.slot_id
        # R4-B1:釋放 notified 候補保留的容量(鎖 slot 後改)。
        slot = _lock_slot(db, slot_id)
        if slot is not None:
            _release_hold(slot, entry)
        entry.status = WAITLIST_CANCELLED
        entry.offer_expires_at = None
        entry.updated_at = _utcnow()
        db.commit()
        db.refresh(entry)
        notify_next_for_slot_best_effort(
            db, tenant_id=tenant_id, slot_id=slot_id
        )
    return entry
