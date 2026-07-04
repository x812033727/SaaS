"""後台預約行事曆檢視（對標 vibeaico「預約行事曆：月曆/週曆、雙模式」）。

雙模式：
  - reservations：顧客預約，依日期分組（月曆格 / 週曆列），狀態以顏色區分。
  - staff：員工排班，員工 × 星期 的班表格（含輪值別）。

純讀取；組裝好結構交給模板渲染。日期一律以 naive date 比較（SQLite 不存時區）。
"""

from __future__ import annotations

import calendar
import datetime

from sqlalchemy.orm import Session

from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.reservation import Reservation
from saas_mvp.services import staff as staff_svc
from saas_mvp.services.tenants import tenant_query

_WEEKDAY_LABELS = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]


def _naive(dt: datetime.datetime) -> datetime.datetime:
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _reservations_in_range(
    db: Session, tenant_id: int, start: datetime.datetime, end: datetime.datetime
) -> list[dict]:
    """[start, end) 內的預約（join slot 取時間），整理為輕量 dict 列。"""
    rows = (
        tenant_query(db, Reservation, tenant_id)
        .join(BookingSlot, Reservation.slot_id == BookingSlot.id)
        .filter(BookingSlot.slot_start >= start, BookingSlot.slot_start < end)
        .with_entities(
            Reservation.id,
            Reservation.status,
            Reservation.party_size,
            Reservation.line_user_id,
            Reservation.attended,
            BookingSlot.slot_start,
        )
        .order_by(BookingSlot.slot_start)
        .all()
    )
    out = []
    for r in rows:
        ss = _naive(r.slot_start)
        out.append({
            "id": r.id,
            "status": r.status,
            "party_size": r.party_size,
            "line_user_id": r.line_user_id,
            "attended": bool(r.attended),
            "slot_start": ss,
            "date": ss.date(),
            "time": ss.strftime("%H:%M"),
        })
    return out


def build_month(db: Session, *, tenant_id: int, year: int, month: int) -> dict:
    """月曆：回傳 weeks（每週 7 天，每天含當日預約列）+ 導覽資訊。"""
    first = datetime.date(year, month, 1)
    days_in_month = calendar.monthrange(year, month)[1]
    start_dt = datetime.datetime(year, month, 1)
    end_dt = start_dt + datetime.timedelta(days=days_in_month)

    by_date: dict[datetime.date, list[dict]] = {}
    for resv in _reservations_in_range(db, tenant_id, start_dt, end_dt):
        by_date.setdefault(resv["date"], []).append(resv)

    # 以「週一起始」的月曆網格鋪排（含跨月補白）。
    cal = calendar.Calendar(firstweekday=0)  # 0 = Monday
    weeks: list[list[dict]] = []
    for week in cal.monthdatescalendar(year, month):
        row = []
        for d in week:
            row.append({
                "date": d,
                "day": d.day,
                "in_month": d.month == month,
                "reservations": by_date.get(d, []),
            })
        weeks.append(row)

    prev_month = (first - datetime.timedelta(days=1)).replace(day=1)
    next_month = (first + datetime.timedelta(days=days_in_month)).replace(day=1)
    return {
        "year": year,
        "month": month,
        "title": f"{year} 年 {month} 月",
        "weekday_labels": _WEEKDAY_LABELS,
        "weeks": weeks,
        "prev": prev_month.isoformat(),
        "next": next_month.isoformat(),
    }


def build_week(db: Session, *, tenant_id: int, anchor: datetime.date) -> dict:
    """週曆：以 anchor 所在週（週一起）回傳 7 天，每天含當日預約列。"""
    monday = anchor - datetime.timedelta(days=anchor.weekday())
    start_dt = datetime.datetime(monday.year, monday.month, monday.day)
    end_dt = start_dt + datetime.timedelta(days=7)

    by_date: dict[datetime.date, list[dict]] = {}
    for resv in _reservations_in_range(db, tenant_id, start_dt, end_dt):
        by_date.setdefault(resv["date"], []).append(resv)

    days = []
    for i in range(7):
        d = monday + datetime.timedelta(days=i)
        days.append({
            "date": d,
            "label": _WEEKDAY_LABELS[i],
            "reservations": by_date.get(d, []),
        })
    return {
        "title": f"{monday.isoformat()} 該週",
        "days": days,
        "prev": (monday - datetime.timedelta(days=7)).isoformat(),
        "next": (monday + datetime.timedelta(days=7)).isoformat(),
    }


def build_staff_grid(db: Session, *, tenant_id: int) -> dict:
    """員工排班格：每位啟用中員工的 7 個 weekday 班表（含輪值別）。"""
    rows = []
    for s in staff_svc.list_staff(db, tenant_id=tenant_id):
        if not s.is_active:
            continue
        per_weekday: list[list[dict]] = [[] for _ in range(7)]
        none_day: list[dict] = []
        for sh in staff_svc.list_shifts(db, tenant_id=tenant_id, staff_id=s.id):
            if not sh.is_active:
                continue
            cell = {
                "start": sh.start_time,
                "end": sh.end_time,
                "rotation": sh.rotation,
            }
            if sh.weekday is None:
                none_day.append(cell)
            elif 0 <= sh.weekday <= 6:
                per_weekday[sh.weekday].append(cell)
        rows.append({
            "name": s.name,
            "role": s.role,
            "weekdays": per_weekday,
            "anyday": none_day,
        })
    return {"weekday_labels": _WEEKDAY_LABELS, "rows": rows}
