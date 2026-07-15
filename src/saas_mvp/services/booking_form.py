"""網頁預約表單服務（A1.1）— tokenized 深連結，比照 services/pii.py 模式。

流程：bot 端 issue_token（僅 WEB_BOOKING 開通時）→ 顧客在 LINE 內建瀏覽器開
``/booking/f/{token}`` → 漸進式選 服務 → 日期 →（員工）→ 時段/人數 → 建單。
身分由 token 攜帶（tenant_id + line_user_id），不需登入、不需 LIFF。

* token 即能力：公開表單以 token 解析（不分租戶）；所有寫入 scope 到該
  token 的 tenant_id。
* 一次性：成功建單標 used_at；要再約回 LINE 重新點按鈕（token 便宜）。
* 建單走既有 booking.book_slot（原子容量、黑名單、候補語意全複用）。
"""

from __future__ import annotations

import datetime
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.models.booking_form_token import BookingFormToken
from saas_mvp.services import catalog as catalog_svc
from saas_mvp.services import features as features_svc
from saas_mvp.services import slots as slots_svc
from saas_mvp.services import staff as staff_svc


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class BookingFormError(Exception):
    """網頁預約表單 domain 錯誤。"""


class TokenNotFound(BookingFormError):
    pass


class TokenExpired(BookingFormError):
    pass


class TokenAlreadyUsed(BookingFormError):
    pass


def issue_token(
    db: Session,
    *,
    tenant_id: int,
    line_user_id: str,
    display_name: str | None = None,
) -> BookingFormToken:
    """發一枚表單 token 並 commit；TTL 取 settings.booking_form_ttl_minutes。"""
    now = _utcnow()
    row = BookingFormToken(
        tenant_id=tenant_id,
        line_user_id=line_user_id,
        display_name=display_name,
        token=secrets.token_urlsafe(32),
        created_at=now,
        expires_at=now + datetime.timedelta(minutes=settings.booking_form_ttl_minutes),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def form_url(row: BookingFormToken) -> str:
    base = settings.public_base_url.rstrip("/")
    return f"{base}/booking/f/{row.token}"


def _is_expired(row: BookingFormToken, now: datetime.datetime | None = None) -> bool:
    now = now or _utcnow()
    exp = row.expires_at
    if exp.tzinfo is None:  # SQLite 取出多為 naive
        now = now.replace(tzinfo=None)
    return now > exp


def resolve_token(db: Session, token: str) -> BookingFormToken:
    """以 token 解析（不分租戶；token 即能力）；異常拋對應 domain error。"""
    row = db.execute(
        select(BookingFormToken).where(BookingFormToken.token == token)
    ).scalar_one_or_none()
    if row is None:
        raise TokenNotFound("token not found")
    if row.used_at is not None:
        raise TokenAlreadyUsed("token already used")
    if _is_expired(row):
        raise TokenExpired("token expired")
    return row


# ── 表單資料組裝（複用 catalog/slots/staff service；邏輯對齊 line_webhook 引導式）──

def active_services(db: Session, tenant_id: int) -> list:
    return [
        s for s in catalog_svc.list_services(db, tenant_id=tenant_id) if s.is_active
    ]


def available_dates(db: Session, tenant_id: int, limit: int = 14) -> list[str]:
    """有啟用時段的日期（含額滿，讓顧客可加入候補）。"""
    seen: set[str] = set()
    today = _utcnow().date()
    for s in slots_svc.list_slots(db, tenant_id=tenant_id, active_only=True):
        if s.slot_start.date() >= today:
            seen.add(s.slot_start.date().isoformat())
    return sorted(seen)[:limit]


def slots_for(
    db: Session, tenant_id: int, *, date: str, service_id: int | None
) -> list:
    """該日期、可容納該服務時長的時段（含額滿候補）。"""
    slots = [
        s
        for s in slots_svc.list_slots(db, tenant_id=tenant_id, active_only=True)
        if s.slot_start.date().isoformat() == date
    ]
    if service_id is None:
        return slots
    try:
        service = catalog_svc.get_service(db, tenant_id=tenant_id, service_id=service_id)
    except Exception:  # noqa: BLE001 — 服務查無：不套過濾
        return slots
    duration = getattr(service, "duration_minutes", None)
    if duration:
        needed = datetime.timedelta(minutes=duration)
        slots = [
            s
            for s in slots
            if s.slot_end is None or (s.slot_end - s.slot_start) >= needed
        ]
    if features_svc.is_enabled(db, tenant_id, features_svc.BOOKABLE_RESOURCES):
        from saas_mvp.services import bookable_resources as resources_svc

        slots = [
            slot
            for slot in slots
            if resources_svc.slot_has_required_resources(
                db,
                tenant_id=tenant_id,
                service_id=service_id,
                slot=slot,
            )
        ]
    return slots


def service_staff(db: Session, tenant_id: int, service_id: int) -> list:
    """指派到該服務的 active 員工（供「指定服務人員」下拉，選填）。"""
    out = []
    for link in catalog_svc.list_service_staff(
        db, tenant_id=tenant_id, service_id=service_id
    ):
        try:
            st = staff_svc.get_staff(db, tenant_id=tenant_id, staff_id=link.staff_id)
        except Exception:  # noqa: BLE001 — 指派但員工已刪：略過
            continue
        if st.is_active:
            out.append(st)
    return out


def submit_booking(
    db: Session,
    *,
    token: str,
    slot_id: int,
    party_size: int,
    service_id: int | None,
    staff_id: int | None,
    use_package: bool = False,
):
    """驗 token → 建單（走既有 book_slot）→ 標記 used。回傳 Reservation。

    book_slot 的 domain error（額滿/黑名單/查無時段）原樣向上拋，由 router
    轉成友善頁面；token 僅在建單成功後才標 used（失敗可重試）。
    """
    from saas_mvp.services import booking as booking_svc

    row = resolve_token(db, token)
    resv = booking_svc.book_slot(
        db,
        tenant_id=row.tenant_id,
        slot_id=slot_id,
        party_size=party_size,
        line_user_id=row.line_user_id,
        display_name=row.display_name,
        staff_id=staff_id,
        service_id=service_id,
        use_package=use_package,
    )
    row.used_at = _utcnow()
    db.commit()
    return resv


def submit_waitlist(
    db: Session,
    *,
    token: str,
    slot_id: int,
    party_size: int,
    service_id: int | None,
    staff_id: int | None,
):
    """以同一枚預約 token 登記候補；token TTL 內可候補多個時段。"""
    from saas_mvp.services import waitlist as waitlist_svc

    row = resolve_token(db, token)
    return waitlist_svc.join_waitlist(
        db,
        tenant_id=row.tenant_id,
        slot_id=slot_id,
        line_user_id=row.line_user_id,
        display_name=row.display_name,
        party_size=party_size,
        service_id=service_id,
        staff_id=staff_id,
    )
