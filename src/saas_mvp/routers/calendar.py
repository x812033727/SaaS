"""行事曆 ICS 訂閱 feed 路由（無認證；token 即能力）。

三種 feed：
  * /calendar/shop/{token}.ics     — 店家整店所有即將到來的預約（tenant.ics_token）
  * /calendar/staff/{token}.ics    — 某員工被指派的預約（staff.access_token，沿用 Phase 1）
  * /calendar/customer/{token}.ics — 某顧客本人的預約（customer.ics_token）

token 解析後，下游讀取仍 scope 到該物件的 tenant_id（資安：token 是唯一憑證，
但解析出的 scope 不得越界讀其他租戶）。未知 token 一律 404。

feed 由當前 DB 狀態即時產生：新增/修改/取消會自動反映；已取消的預約以
STATUS:CANCELLED 輸出（METHOD:CANCEL）。
include_in_schema=False：不對外暴露於 OpenAPI（內部訂閱用途）。
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.db import get_db
from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.customer import Customer
from saas_mvp.models.reservation import (
    RESERVATION_CANCELLED,
    Reservation,
)
from saas_mvp.models.staff import Staff
from saas_mvp.models.tenant import Tenant
from saas_mvp.services.calendar_ics import build_ics

router = APIRouter(prefix="/calendar", tags=["calendar"], include_in_schema=False)


def _reservation_to_event(
    resv: Reservation, slot: BookingSlot, *, store_name: str
) -> dict:
    """把一筆 reservation + slot 轉成 build_ics 可吃的 event dict。

    SEQUENCE 由 updated_at epoch 推導（每次修改後遞增，提示行事曆更新）。
    cancelled 預約輸出 status='cancelled' → STATUS:CANCELLED。
    """
    start = slot.slot_start
    end = slot.slot_end or (start + datetime.timedelta(hours=1))
    updated = resv.updated_at or resv.created_at or start
    sequence = int(updated.timestamp()) if updated else 0
    status_str = (
        "cancelled" if resv.status == RESERVATION_CANCELLED else "confirmed"
    )
    summary = f"{store_name} 預約 #{resv.id}"
    desc_parts = [f"人數：{resv.party_size} 位"]
    if resv.note:
        desc_parts.append(f"備註：{resv.note}")
    return {
        "uid": f"resv-{resv.id}@saas-mvp",
        "summary": summary,
        "start": start,
        "end": end,
        "status": status_str,
        "sequence": sequence,
        "description": "；".join(desc_parts),
        "updated_at": updated,
    }


def _build_feed(
    db: Session,
    *,
    tenant_id: int,
    reservations: list[Reservation],
    store_name: str,
) -> str:
    """為一組 reservation 載入對應 slot 並組出 ICS 文字。"""
    events: list[dict] = []
    for resv in reservations:
        slot = (
            db.query(BookingSlot)
            .filter(
                BookingSlot.id == resv.slot_id,
                BookingSlot.tenant_id == tenant_id,
            )
            .first()
        )
        if slot is None:
            continue
        events.append(
            _reservation_to_event(resv, slot, store_name=store_name)
        )
    ics = build_ics(events)
    return ics


def _ics_response(ics: str) -> Response:
    return Response(content=ics, media_type="text/calendar")


@router.get("/shop/{token}.ics")
def shop_feed(token: str, db: Session = Depends(get_db)) -> Response:
    tenant = db.execute(
        select(Tenant).where(Tenant.ics_token == token)
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Feed not found"
        )
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=1)
    rows = list(
        db.execute(
            select(Reservation)
            .join(BookingSlot, BookingSlot.id == Reservation.slot_id)
            .where(
                Reservation.tenant_id == tenant.id,
                BookingSlot.slot_start >= cutoff,
            )
            .order_by(BookingSlot.slot_start)
        ).scalars()
    )
    ics = _build_feed(
        db, tenant_id=tenant.id, reservations=rows, store_name=tenant.name
    )
    return _ics_response(ics)


@router.get("/staff/{token}.ics")
def staff_feed(token: str, db: Session = Depends(get_db)) -> Response:
    staff = db.execute(
        select(Staff).where(Staff.access_token == token)
    ).scalar_one_or_none()
    if staff is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Feed not found"
        )
    tenant = db.get(Tenant, staff.tenant_id)
    store_name = tenant.name if tenant is not None else ""
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=1)
    rows = list(
        db.execute(
            select(Reservation)
            .join(BookingSlot, BookingSlot.id == Reservation.slot_id)
            .where(
                Reservation.tenant_id == staff.tenant_id,
                Reservation.staff_id == staff.id,
                BookingSlot.slot_start >= cutoff,
            )
            .order_by(BookingSlot.slot_start)
        ).scalars()
    )
    ics = _build_feed(
        db, tenant_id=staff.tenant_id, reservations=rows, store_name=store_name
    )
    return _ics_response(ics)


@router.get("/customer/{token}.ics")
def customer_feed(token: str, db: Session = Depends(get_db)) -> Response:
    customer = db.execute(
        select(Customer).where(Customer.ics_token == token)
    ).scalar_one_or_none()
    if customer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Feed not found"
        )
    tenant = db.get(Tenant, customer.tenant_id)
    store_name = tenant.name if tenant is not None else ""
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=1)
    rows = list(
        db.execute(
            select(Reservation)
            .join(BookingSlot, BookingSlot.id == Reservation.slot_id)
            .where(
                Reservation.tenant_id == customer.tenant_id,
                Reservation.customer_id == customer.id,
                BookingSlot.slot_start >= cutoff,
            )
            .order_by(BookingSlot.slot_start)
        ).scalars()
    )
    ics = _build_feed(
        db,
        tenant_id=customer.tenant_id,
        reservations=rows,
        store_name=store_name,
    )
    return _ics_response(ics)
