"""員工排班（staff scheduling）服務層 — CRUD + 排班衝突判定 + 原子指派。

跨租戶一律走 tenant_query / 帶 tenant_id 條件；查無回 404（CRUD 拋 HTTPException）。
指派（assign_staff）為多寫路徑，比照 booking.book_slot：SELECT … FOR UPDATE 鎖
reservation 列 → 鎖內判衝突 → 設 staff_id → 單一 commit。

access_token 為員工自助入口憑證（capability）：resolve_by_token 不套租戶 filter。
"""

from __future__ import annotations

import datetime
import secrets

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.location import Location
from saas_mvp.models.reservation import RESERVATION_CONFIRMED, Reservation
from saas_mvp.models.staff import VALID_STAFF_MODES, Staff
from saas_mvp.models.staff_leave import StaffLeave
from saas_mvp.models.staff_shift import StaffShift
from saas_mvp.services.tenants import tenant_query


def _assert_location_owned(db: Session, tenant_id: int, location_id: int | None) -> None:
    """location_id 若帶入，須屬於本租戶，否則 422（防跨租戶引用）。"""
    if location_id is None:
        return
    owned = (
        tenant_query(db, Location, tenant_id)
        .filter(Location.id == location_id)
        .first()
    )
    if owned is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="location_id 不屬於本租戶",
        )


# ── 自訂例外（router 轉 HTTP） ────────────────────────────────────────────────
class StaffError(Exception):
    """員工排班錯誤基底。"""


class StaffNotFoundError(StaffError):
    """員工不存在或跨租戶。"""


class StaffConflictError(StaffError):
    """指派衝突（請假 / 非班表時段 / 已被佔用）。"""


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def generate_access_token() -> str:
    return secrets.token_urlsafe(32)


def _naive(dt: datetime.datetime | None) -> datetime.datetime | None:
    """SQLite 讀回為 naive；比較前統一去 tzinfo 避免 aware/naive 混比。"""
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _hhmm(dt: datetime.datetime) -> str:
    return f"{dt.hour:02d}:{dt.minute:02d}"


# ── 員工 CRUD ─────────────────────────────────────────────────────────────────

def _get_or_404(db: Session, tenant_id: int, staff_id: int) -> Staff:
    staff = (
        tenant_query(db, Staff, tenant_id).filter(Staff.id == staff_id).first()
    )
    if staff is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Staff not found"
        )
    return staff


def list_staff(db: Session, *, tenant_id: int) -> list[Staff]:
    return tenant_query(db, Staff, tenant_id).order_by(Staff.id).all()


def get_staff(db: Session, *, tenant_id: int, staff_id: int) -> Staff:
    return _get_or_404(db, tenant_id, staff_id)


def _enforce_staff_limit(db: Session, tenant_id: int) -> None:
    """免費版員工數上限閘門：未開通 UNLIMITED_STAFF 時，啟用中員工數不得超過
    settings.free_staff_limit（預設 3）。對標 vibeaico「無限員工」進階功能。

    僅計「啟用中」（is_active=True）員工——停用者不佔額度。
    開通（輕量版以上）後完全不限。
    """
    # 延遲 import 避免 services.features ↔ auth.dependencies 載入期循環。
    from saas_mvp.config import settings
    from saas_mvp.services import features as features_svc

    if features_svc.is_enabled(db, tenant_id, features_svc.UNLIMITED_STAFF):
        return
    active_count = (
        tenant_query(db, Staff, tenant_id)
        .filter(Staff.is_active.is_(True))
        .count()
    )
    if active_count >= settings.free_staff_limit:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"免費版員工上限為 {settings.free_staff_limit} 位。"
                "升級「無限員工」(輕量版以上) 即可解除上限。"
            ),
        )


def create_staff(
    db: Session,
    *,
    tenant_id: int,
    name: str,
    role: str | None = None,
    location_id: int | None = None,
    booking_mode: str = "capacity",
) -> Staff:
    if booking_mode not in VALID_STAFF_MODES:
        raise HTTPException(
            status_code=422, detail=f"Invalid booking_mode: {booking_mode!r}"
        )
    _enforce_staff_limit(db, tenant_id)
    _assert_location_owned(db, tenant_id, location_id)
    staff = Staff(
        tenant_id=tenant_id,
        name=name,
        role=role,
        location_id=location_id,
        booking_mode=booking_mode,
        # 建立即發 capability token，員工專屬連結 /s/{token} 開箱即用
        # （rotate_token 仍可重新產生作廢舊連結）。
        access_token=generate_access_token(),
    )
    db.add(staff)
    db.commit()
    db.refresh(staff)
    return staff


def update_staff(
    db: Session,
    *,
    tenant_id: int,
    staff_id: int,
    name: str | None = None,
    role: str | None = None,
    location_id: int | None = None,
    booking_mode: str | None = None,
    is_active: bool | None = None,
) -> Staff:
    staff = _get_or_404(db, tenant_id, staff_id)
    if name is not None:
        staff.name = name
    if role is not None:
        staff.role = role
    if location_id is not None:
        _assert_location_owned(db, tenant_id, location_id)
        staff.location_id = location_id
    if booking_mode is not None:
        if booking_mode not in VALID_STAFF_MODES:
            raise HTTPException(
                status_code=422, detail=f"Invalid booking_mode: {booking_mode!r}"
            )
        staff.booking_mode = booking_mode
    if is_active is not None:
        staff.is_active = is_active
    db.commit()
    db.refresh(staff)
    return staff


def delete_staff(db: Session, *, tenant_id: int, staff_id: int) -> None:
    """刪除員工；其班表/請假/服務指派由 DB FK ondelete=CASCADE 連帶清除。"""
    staff = _get_or_404(db, tenant_id, staff_id)
    db.delete(staff)
    db.commit()


def rotate_token(db: Session, *, tenant_id: int, staff_id: int) -> Staff:
    """重新產生員工自助入口憑證。"""
    staff = _get_or_404(db, tenant_id, staff_id)
    staff.access_token = generate_access_token()
    db.commit()
    db.refresh(staff)
    return staff


def resolve_by_token(db: Session, token: str) -> Staff | None:
    """以 access_token 解析員工——**不套租戶 filter**（token 即能力）。"""
    if not token:
        return None
    return db.execute(
        select(Staff).where(Staff.access_token == token)
    ).scalar_one_or_none()


# ── 班表 CRUD ─────────────────────────────────────────────────────────────────

def list_shifts(db: Session, *, tenant_id: int, staff_id: int) -> list[StaffShift]:
    _get_or_404(db, tenant_id, staff_id)
    return (
        tenant_query(db, StaffShift, tenant_id)
        .filter(StaffShift.staff_id == staff_id)
        .order_by(StaffShift.id)
        .all()
    )


def create_shift(
    db: Session,
    *,
    tenant_id: int,
    staff_id: int,
    start_time: str,
    end_time: str,
    weekday: int | None = None,
    rotation: str | None = None,
) -> StaffShift:
    _get_or_404(db, tenant_id, staff_id)
    if weekday is not None and not (0 <= weekday <= 6):
        raise HTTPException(status_code=422, detail="weekday must be 0-6")
    shift = StaffShift(
        tenant_id=tenant_id,
        staff_id=staff_id,
        weekday=weekday,
        start_time=start_time,
        end_time=end_time,
        rotation=rotation,
    )
    db.add(shift)
    from sqlalchemy.exc import IntegrityError

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A shift already exists for this staff/weekday/start_time",
        )
    db.refresh(shift)
    return shift


# 內建班表模板（對標 vibeaico「內建模板一鍵套用」）。
SHIFT_TEMPLATES: dict[str, dict] = {
    "early": {"label": "早班", "start": "09:00", "end": "13:00", "rotation": "day"},
    "late": {"label": "晚班", "start": "14:00", "end": "18:00", "rotation": "night"},
    "fullday": {"label": "全日班", "start": "09:00", "end": "18:00", "rotation": "day"},
}


def bulk_create_shifts(
    db: Session,
    *,
    tenant_id: int,
    staff_id: int,
    weekdays: list[int],
    start_time: str,
    end_time: str,
    rotation: str | None = None,
) -> dict:
    """為一名員工在多個 weekday 批量建立相同時段班表（對標 vibeaico 批量排班）。

    冪等：(staff_id, weekday, start_time) 已存在者略過。回傳 {created, skipped}。
    """
    _get_or_404(db, tenant_id, staff_id)
    if not weekdays:
        raise HTTPException(status_code=422, detail="weekdays must not be empty")
    for wd in weekdays:
        if not (0 <= wd <= 6):
            raise HTTPException(status_code=422, detail="weekday must be 0-6")
    if not start_time or not end_time:
        raise HTTPException(status_code=422, detail="start_time/end_time required")

    existing = {
        (sh.weekday, sh.start_time)
        for sh in list_shifts(db, tenant_id=tenant_id, staff_id=staff_id)
    }
    created = 0
    skipped = 0
    for wd in sorted(set(weekdays)):
        if (wd, start_time) in existing:
            skipped += 1
            continue
        db.add(StaffShift(
            tenant_id=tenant_id,
            staff_id=staff_id,
            weekday=wd,
            start_time=start_time,
            end_time=end_time,
            rotation=rotation,
        ))
        created += 1
    db.commit()
    return {"created": created, "skipped": skipped}


def bulk_create_shifts_from_template(
    db: Session,
    *,
    tenant_id: int,
    staff_id: int,
    template: str,
    weekdays: list[int],
) -> dict:
    """以內建模板批量排班；未知模板 → 422。"""
    tpl = SHIFT_TEMPLATES.get(template)
    if tpl is None:
        raise HTTPException(
            status_code=422, detail=f"Unknown shift template: {template!r}"
        )
    return bulk_create_shifts(
        db,
        tenant_id=tenant_id,
        staff_id=staff_id,
        weekdays=weekdays,
        start_time=tpl["start"],
        end_time=tpl["end"],
        rotation=tpl["rotation"],
    )


def delete_shift(db: Session, *, tenant_id: int, staff_id: int, shift_id: int) -> None:
    _get_or_404(db, tenant_id, staff_id)
    shift = (
        tenant_query(db, StaffShift, tenant_id)
        .filter(StaffShift.id == shift_id, StaffShift.staff_id == staff_id)
        .first()
    )
    if shift is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Shift not found"
        )
    db.delete(shift)
    db.commit()


# ── 請假 CRUD ─────────────────────────────────────────────────────────────────

def list_leaves(db: Session, *, tenant_id: int, staff_id: int) -> list[StaffLeave]:
    _get_or_404(db, tenant_id, staff_id)
    return (
        tenant_query(db, StaffLeave, tenant_id)
        .filter(StaffLeave.staff_id == staff_id)
        .order_by(StaffLeave.id)
        .all()
    )


def create_leave(
    db: Session,
    *,
    tenant_id: int,
    staff_id: int,
    start_at: datetime.datetime,
    end_at: datetime.datetime,
    reason: str | None = None,
    status_value: str = "approved",
) -> StaffLeave:
    _get_or_404(db, tenant_id, staff_id)
    leave = StaffLeave(
        tenant_id=tenant_id,
        staff_id=staff_id,
        start_at=start_at,
        end_at=end_at,
        reason=reason,
        status=status_value,
    )
    db.add(leave)
    db.commit()
    db.refresh(leave)
    return leave


def delete_leave(db: Session, *, tenant_id: int, staff_id: int, leave_id: int) -> None:
    _get_or_404(db, tenant_id, staff_id)
    leave = (
        tenant_query(db, StaffLeave, tenant_id)
        .filter(StaffLeave.id == leave_id, StaffLeave.staff_id == staff_id)
        .first()
    )
    if leave is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Leave not found"
        )
    db.delete(leave)
    db.commit()


# ── 衝突判定 ──────────────────────────────────────────────────────────────────

def check_conflict(
    db: Session,
    *,
    tenant_id: int,
    staff_id: int,
    start_at: datetime.datetime,
    end_at: datetime.datetime,
    exclude_reservation_id: int | None = None,
) -> tuple[bool, str | None]:
    """判定員工在 [start_at, end_at) 是否可被指派。

    回 (ok, reason)。衝突來源（任一觸發即衝突）：
      1. 有 approved 請假與該區間重疊。
      2. 該 weekday 有定義 active 非 'off' 班表，但時刻落在所有班表之外。
      3. 已有另一筆 confirmed 預約（同員工）時段與該區間重疊。
    """
    s = _naive(start_at)
    e = _naive(end_at)

    # 1) 請假重疊（approved）
    leaves = (
        tenant_query(db, StaffLeave, tenant_id)
        .filter(
            StaffLeave.staff_id == staff_id,
            StaffLeave.status == "approved",
        )
        .all()
    )
    for lv in leaves:
        ls = _naive(lv.start_at)
        le = _naive(lv.end_at)
        if ls is None or le is None:
            continue
        if s < le and ls < e:  # 區間重疊
            return False, "staff on leave"

    # 2) 班表覆蓋（僅當該 weekday 有定義 active 非 off 班表時才強制）
    weekday = s.weekday()
    shifts = (
        tenant_query(db, StaffShift, tenant_id)
        .filter(
            StaffShift.staff_id == staff_id,
            StaffShift.is_active.is_(True),
        )
        .all()
    )
    day_shifts = [
        sh
        for sh in shifts
        if (sh.weekday is None or sh.weekday == weekday)
        and (sh.rotation or "") != "off"
    ]
    if day_shifts:
        start_hhmm = _hhmm(s)
        end_hhmm = _hhmm(e)
        covered = any(
            sh.start_time <= start_hhmm and end_hhmm <= sh.end_time
            for sh in day_shifts
        )
        if not covered:
            return False, "outside staff shift"

    # 3) 既有 confirmed 預約時段重疊（同員工）
    rows = (
        tenant_query(db, Reservation, tenant_id)
        .filter(
            Reservation.staff_id == staff_id,
            Reservation.status == RESERVATION_CONFIRMED,
        )
        .all()
    )
    for resv in rows:
        if exclude_reservation_id is not None and resv.id == exclude_reservation_id:
            continue
        slot = (
            tenant_query(db, BookingSlot, tenant_id)
            .filter(BookingSlot.id == resv.slot_id)
            .first()
        )
        if slot is None:
            continue
        rs = _naive(slot.slot_start)
        re = _naive(slot.slot_end) or rs
        if rs is None:
            continue
        if s < re and rs < e:  # 重疊
            return False, "staff already booked"

    return True, None


# ── 原子指派 ──────────────────────────────────────────────────────────────────

def assign_staff(
    db: Session,
    *,
    tenant_id: int,
    reservation_id: int,
    staff_id: int,
) -> Reservation:
    """把員工指派給某預約：鎖預約列 → 取其時段時間判衝突 → 設 staff_id → commit。

    衝突拋 StaffConflictError；預約/員工不存在拋對應例外。
    """
    # 先鎖共用的 Staff 列（FOR UPDATE）：跨「兩筆不同預約」指派同一員工的
    # 重疊檢查（check_conflict 第 3 條）是跨 reservation 的不變式，只鎖目標
    # reservation 無法序列化它——兩個並發指派各鎖自己的 reservation，雙雙
    # 看不到對方、可同時通過衝突檢查而雙重佔用。鎖 staff 列讓「同一員工」的
    # 所有指派序列化（check_conflict → set staff_id → commit 在鎖內完成）。
    staff = db.execute(
        select(Staff)
        .where(Staff.id == staff_id, Staff.tenant_id == tenant_id)
        .with_for_update()
    ).scalar_one_or_none()
    if staff is None:
        raise StaffNotFoundError(f"staff {staff_id} not found")

    reservation = db.execute(
        select(Reservation)
        .where(
            Reservation.id == reservation_id,
            Reservation.tenant_id == tenant_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if reservation is None:
        raise StaffNotFoundError(f"reservation {reservation_id} not found")

    slot = (
        tenant_query(db, BookingSlot, tenant_id)
        .filter(BookingSlot.id == reservation.slot_id)
        .first()
    )
    if slot is None:
        raise StaffNotFoundError(f"slot for reservation {reservation_id} not found")

    start_at = slot.slot_start
    end_at = slot.slot_end or slot.slot_start
    ok, reason = check_conflict(
        db,
        tenant_id=tenant_id,
        staff_id=staff_id,
        start_at=start_at,
        end_at=end_at,
        exclude_reservation_id=reservation_id,
    )
    if not ok:
        raise StaffConflictError(reason or "staff conflict")

    reservation.staff_id = staff_id
    db.commit()
    db.refresh(reservation)
    return reservation
