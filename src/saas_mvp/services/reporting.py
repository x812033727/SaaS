"""進階報表服務（PHASE 4-2）— 熱門服務 / 員工績效 / 營收趨勢 / 回購率 + 匯出。

設計沿用 services/analytics.py：
* 租戶隔離（所有查詢帶 tenant_id）+ 選填 location_id 範圍。
* 取資料後於 Python 聚合（避免 DB 方言差異；單租戶資料量適中）。
* 日期區間：預約以 BookingSlot.slot_start 過濾；訂單以 paid_at（已付）過濾。

員工績效營收：訂單（Order）未綁定 staff，故以該員工已確認預約對應服務的 price_cents
近似（誠實標註為服務定價推估，非實收）。實收營收見 revenue_trend（以已付訂單為準）。

匯出：to_xlsx / to_pdf 以 lazy import 載入 openpyxl / fpdf，模組本身不硬依賴，
非匯出測試免裝這兩個套件。
"""

from __future__ import annotations

import datetime
import io

from sqlalchemy import String, case, cast, func, literal, or_, select
from sqlalchemy.orm import Session

from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.order import ORDER_PAID, Order
from saas_mvp.models.reservation import RESERVATION_CONFIRMED, Reservation
from saas_mvp.models.service import Service
from saas_mvp.models.staff import Staff


def _confirmed_reservations_stmt(
    tenant_id: int,
    date_from: datetime.datetime | None,
    date_to: datetime.datetime | None,
    location_id: int | None,
):
    stmt = (
        select(Reservation)
        .join(BookingSlot, Reservation.slot_id == BookingSlot.id)
        .where(
            Reservation.tenant_id == tenant_id,
            Reservation.status == RESERVATION_CONFIRMED,
        )
    )
    if date_from is not None:
        stmt = stmt.where(BookingSlot.slot_start >= date_from)
    if date_to is not None:
        stmt = stmt.where(BookingSlot.slot_start <= date_to)
    if location_id is not None:
        # 分店範圍：以服務的 location_id 推導（Slot/Reservation 未直接綁分店）。
        # join 必帶 Service.tenant_id 條件，防跨租戶 Service 列混入。
        stmt = stmt.join(
            Service,
            (Reservation.service_id == Service.id)
            & (Service.tenant_id == tenant_id),
        ).where(Service.location_id == location_id)
    return stmt


def _confirmed_reservations(
    db: Session,
    tenant_id: int,
    date_from: datetime.datetime | None,
    date_to: datetime.datetime | None,
    location_id: int | None,
) -> list[Reservation]:
    return list(
        db.execute(
            _confirmed_reservations_stmt(
                tenant_id, date_from, date_to, location_id
            )
        ).scalars()
    )


def _service_names(db: Session, tenant_id: int) -> dict[int, str]:
    rows = db.execute(
        select(Service.id, Service.name).where(Service.tenant_id == tenant_id)
    ).all()
    return {sid: name for sid, name in rows}


def _service_prices(db: Session, tenant_id: int) -> dict[int, int]:
    rows = db.execute(
        select(Service.id, Service.price_cents).where(Service.tenant_id == tenant_id)
    ).all()
    return {sid: (price or 0) for sid, price in rows}


def _staff_names(db: Session, tenant_id: int) -> dict[int, str]:
    rows = db.execute(
        select(Staff.id, Staff.name).where(Staff.tenant_id == tenant_id)
    ).all()
    return {sid: name for sid, name in rows}


def popular_services(
    db: Session,
    *,
    tenant_id: int,
    date_from: datetime.datetime | None = None,
    date_to: datetime.datetime | None = None,
    location_id: int | None = None,
) -> list[dict]:
    """依預約數排名的熱門服務（confirmed 預約，依 service_id 分組，附服務名稱）。"""
    rows = _confirmed_reservations(db, tenant_id, date_from, date_to, location_id)
    names = _service_names(db, tenant_id)
    counts: dict[int, int] = {}
    for r in rows:
        if r.service_id is None:
            continue
        counts[r.service_id] = counts.get(r.service_id, 0) + 1
    out = [
        {
            "service_id": sid,
            "service_name": names.get(sid, f"服務#{sid}"),
            "reservation_count": cnt,
        }
        for sid, cnt in counts.items()
    ]
    # 依預約數降冪、再依 service_id 穩定排序。
    out.sort(key=lambda d: (-d["reservation_count"], d["service_id"]))
    return out


def staff_performance(
    db: Session,
    *,
    tenant_id: int,
    date_from: datetime.datetime | None = None,
    date_to: datetime.datetime | None = None,
    location_id: int | None = None,
) -> list[dict]:
    """每員工：confirmed 預約數 + 服務定價推估營收（cents）。"""
    rows = _confirmed_reservations(db, tenant_id, date_from, date_to, location_id)
    names = _staff_names(db, tenant_id)
    prices = _service_prices(db, tenant_id)
    agg: dict[int, dict] = {}
    for r in rows:
        if r.staff_id is None:
            continue
        b = agg.setdefault(
            r.staff_id,
            {"reservation_count": 0, "revenue_cents": 0, "attended": 0, "no_show": 0},
        )
        b["reservation_count"] += 1
        if r.service_id is not None:
            b["revenue_cents"] += prices.get(r.service_id, 0)
        if r.attended is True:
            b["attended"] += 1
        elif r.attended is False:
            b["no_show"] += 1
    out = [
        {
            "staff_id": sid,
            "staff_name": names.get(sid, f"員工#{sid}"),
            "reservation_count": v["reservation_count"],
            "revenue_cents": v["revenue_cents"],
            "attended": v["attended"],
            "no_show": v["no_show"],
            # 出席率:僅在有標記到場/未到時才有值(誠實呈現)
            "attendance_rate": (
                round(v["attended"] / (v["attended"] + v["no_show"]), 4)
                if (v["attended"] + v["no_show"]) else None
            ),
        }
        for sid, v in agg.items()
    ]
    out.sort(key=lambda d: (-d["reservation_count"], d["staff_id"]))
    return out


def revenue_trend(
    db: Session,
    *,
    tenant_id: int,
    date_from: datetime.datetime | None = None,
    date_to: datetime.datetime | None = None,
    location_id: int | None = None,
) -> list[dict]:
    """已付訂單營收依「日」分桶（依 paid_at）。location_id 在此忽略（訂單未綁分店）。

    聚合下推 SQL（GROUP BY date(paid_at)），免撈全表回 Python。
    方言注意：SQLite 的 CAST(x AS DATE) 語意錯誤（回傳前導數字），
    須用 func.date()（SQLite 原生 date()、PG 的 date() 函式式 cast 皆可）。
    """
    day = func.date(Order.paid_at).label("day")
    stmt = (
        select(
            day,
            func.count(Order.id),
            func.coalesce(func.sum(Order.total_cents), 0),
        )
        .where(
            Order.tenant_id == tenant_id,
            Order.status == ORDER_PAID,
            Order.paid_at.is_not(None),
        )
        .group_by(day)
        .order_by(day)
    )
    if date_from is not None:
        stmt = stmt.where(Order.paid_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(Order.paid_at <= date_to)
    return [
        {
            # SQLite 回字串、PG 回 date 物件；str() 皆得 ISO 格式
            "day": str(d),
            "order_count": int(cnt),
            "revenue_cents": int(total),
        }
        for d, cnt, total in db.execute(stmt).all()
    ]


def return_rate(
    db: Session,
    *,
    tenant_id: int,
    date_from: datetime.datetime | None = None,
    date_to: datetime.datetime | None = None,
    location_id: int | None = None,
) -> dict:
    """回購率：窗內有 ≥2 筆 confirmed 預約的顧客 / 窗內有預約的顧客總數。

    聚合下推 SQL：以顧客鍵（line_user_id，缺者退回 'resv:{customer_id}'）
    GROUP BY 後在外層一趟算 total / repeat。
    """
    ckey = func.coalesce(
        Reservation.line_user_id,
        literal("resv:").concat(cast(Reservation.customer_id, String)),
    ).label("ckey")
    per_customer = (
        _confirmed_reservations_stmt(
            tenant_id, date_from, date_to, location_id
        )
        .with_only_columns(ckey, func.count().label("n"))
        .where(
            or_(
                Reservation.line_user_id.is_not(None),
                Reservation.customer_id.is_not(None),
            )
        )
        .group_by(ckey)
        .subquery()
    )
    total, repeat = db.execute(
        select(
            func.count(),
            func.coalesce(
                func.sum(case((per_customer.c.n >= 2, 1), else_=0)), 0
            ),
        ).select_from(per_customer)
    ).one()
    total = int(total)
    repeat = int(repeat)
    return {
        "total_customers": total,
        "repeat_customers": repeat,
        "return_rate": round(repeat / total, 4) if total else 0.0,
    }


# ── 匯出（lazy import openpyxl / fpdf；模組本身不硬依賴） ──────────────────────


def to_xlsx(report_rows: list[dict], *, sheet_title: str = "Report") -> bytes:
    """把扁平列（list[dict]）寫成 xlsx bytes。空列表也產生只有表頭/空白的有效檔。"""
    from openpyxl import Workbook  # lazy

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title[:31] or "Report"
    if report_rows:
        headers = list(report_rows[0].keys())
        ws.append(headers)
        for row in report_rows:
            ws.append([row.get(h, "") for h in headers])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def to_pdf(report_rows: list[dict], *, title: str = "Report") -> bytes:
    """把扁平列（list[dict]）渲染成簡單表格 PDF bytes。"""
    from fpdf import FPDF  # lazy
    from fpdf.enums import XPos, YPos  # lazy

    def _cell(pdf, h, txt):
        pdf.cell(0, h, txt, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=14)
    _cell(pdf, 10, title)
    pdf.set_font("Helvetica", size=9)
    if report_rows:
        headers = list(report_rows[0].keys())
        _cell(pdf, 8, " | ".join(str(h) for h in headers))
        for row in report_rows:
            line = " | ".join(str(row.get(h, "")) for h in headers)
            # 避免 latin-1 編碼錯誤（fpdf 核心字型不支援非 latin-1）。
            line = line.encode("latin-1", "replace").decode("latin-1")
            _cell(pdf, 7, line)
    else:
        _cell(pdf, 8, "(no data)")
    out = pdf.output()
    return bytes(out)
