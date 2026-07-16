"""一鍵示範資料(R4-B4)— 建立可立即操作的樣本,幫新店家看懂系統怎麼運作。

`load_demo` 建立 1 服務 + 3 個未來時段 + 1 位示範顧客 + 1 筆示範預約,每筆登記於
``tenant_demo_objects``。`clear_demo` 只刪登記過的物件,且拒絕刪除已被「真實預約」
引用者(避免 FK 連鎖刪到店家真資料),回報實刪與保留數。刻意不走 booking 服務:
不觸發 LINE 通知 / Google 日曆同步 —— 純樣本,side-effect free。

以「示範」前綴命名,店家一眼可辨;上線前一鍵清除即可。
"""

from __future__ import annotations

import datetime
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.customer import Customer
from saas_mvp.models.reservation import RESERVATION_CONFIRMED, Reservation
from saas_mvp.models.service import Service
from saas_mvp.models.tenant_demo_object import (
    DEMO_CUSTOMER,
    DEMO_RESERVATION,
    DEMO_SERVICE,
    DEMO_SLOT,
    TenantDemoObject,
)

_PREFIX = "【示範】"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def has_demo(db: Session, tenant_id: int) -> bool:
    return db.execute(
        select(TenantDemoObject.id)
        .where(TenantDemoObject.tenant_id == tenant_id)
        .limit(1)
    ).first() is not None


def _track(db: Session, tenant_id: int, object_type: str, object_id: int) -> None:
    db.add(
        TenantDemoObject(
            tenant_id=tenant_id, object_type=object_type, object_id=object_id
        )
    )


def load_demo(db: Session, tenant_id: int, *, now: datetime.datetime | None = None) -> dict:
    """建立追蹤式示範資料;已存在則不重複建立(冪等)。回建立計數。"""
    if has_demo(db, tenant_id):
        return {"created": False, "reason": "already_loaded"}

    effective_now = (now or _utcnow()).replace(tzinfo=None)
    # 對齊到下一個整點,樣本時段看起來乾淨。
    base = effective_now.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)

    service = Service(
        tenant_id=tenant_id,
        name=f"{_PREFIX}體驗諮詢",
        duration_minutes=60,
        price_cents=0,
        is_active=True,
    )
    db.add(service)
    db.flush()
    _track(db, tenant_id, DEMO_SERVICE, service.id)

    slots: list[BookingSlot] = []
    for day in (1, 2, 3):
        start = base + datetime.timedelta(days=day)
        slot = BookingSlot(
            tenant_id=tenant_id,
            slot_start=start,
            slot_end=start + datetime.timedelta(minutes=60),
            max_capacity=5,
            booked_count=0,
            is_active=True,
        )
        db.add(slot)
        db.flush()
        _track(db, tenant_id, DEMO_SLOT, slot.id)
        slots.append(slot)

    customer = Customer(
        tenant_id=tenant_id,
        display_name=f"{_PREFIX}王小明",
        phone="0900000000",
        note="示範顧客,可直接刪除",
    )
    db.add(customer)
    db.flush()
    _track(db, tenant_id, DEMO_CUSTOMER, customer.id)

    first = slots[0]
    reservation = Reservation(
        tenant_id=tenant_id,
        slot_id=first.id,
        customer_id=customer.id,
        service_id=service.id,
        party_size=2,
        status=RESERVATION_CONFIRMED,
        note="示範預約,熟悉操作後可直接取消或清除示範資料",
    )
    db.add(reservation)
    db.flush()
    first.booked_count = (first.booked_count or 0) + reservation.party_size
    _track(db, tenant_id, DEMO_RESERVATION, reservation.id)

    db.commit()
    return {
        "created": True,
        "service": 1,
        "slot": len(slots),
        "customer": 1,
        "reservation": 1,
    }


def clear_demo(db: Session, tenant_id: int) -> dict:
    """刪除登記的示範物件;被真實預約引用者保留。回實刪與保留計數。"""
    objs = db.execute(
        select(TenantDemoObject).where(TenantDemoObject.tenant_id == tenant_id)
    ).scalars().all()
    if not objs:
        return {"cleared": False, "reason": "no_demo"}

    by_type: dict[str, list[int]] = defaultdict(list)
    for o in objs:
        by_type[o.object_type].append(o.object_id)

    removed = {DEMO_RESERVATION: 0, DEMO_SLOT: 0, DEMO_CUSTOMER: 0, DEMO_SERVICE: 0}
    kept = {DEMO_SLOT: 0, DEMO_CUSTOMER: 0, DEMO_SERVICE: 0}
    demo_resv_ids = set(by_type.get(DEMO_RESERVATION, []))

    # 1) 示範預約:一律刪除,並回補其時段的 booked_count。
    for rid in demo_resv_ids:
        r = db.get(Reservation, rid)
        if r is None:
            continue
        slot = db.get(BookingSlot, r.slot_id)
        if slot is not None:
            slot.booked_count = max(0, (slot.booked_count or 0) - (r.party_size or 0))
        db.delete(r)
        removed[DEMO_RESERVATION] += 1
    db.flush()

    # 2) 示範時段:僅在無任何殘留預約(真實預約)時刪除,否則保留避免 CASCADE 刪真資料。
    for sid in by_type.get(DEMO_SLOT, []):
        slot = db.get(BookingSlot, sid)
        if slot is None:
            continue
        remaining = db.execute(
            select(func.count(Reservation.id)).where(Reservation.slot_id == sid)
        ).scalar_one()
        if remaining == 0:
            db.delete(slot)
            removed[DEMO_SLOT] += 1
        else:
            kept[DEMO_SLOT] += 1

    # 3) 示範顧客:僅在無任何預約引用時刪除。
    for cid in by_type.get(DEMO_CUSTOMER, []):
        cust = db.get(Customer, cid)
        if cust is None:
            continue
        refs = db.execute(
            select(func.count(Reservation.id)).where(Reservation.customer_id == cid)
        ).scalar_one()
        if refs == 0:
            db.delete(cust)
            removed[DEMO_CUSTOMER] += 1
        else:
            kept[DEMO_CUSTOMER] += 1

    # 4) 示範服務:僅在無任何預約引用時刪除(service_id 無 FK,避免留懸空引用)。
    for svid in by_type.get(DEMO_SERVICE, []):
        svc = db.get(Service, svid)
        if svc is None:
            continue
        refs = db.execute(
            select(func.count(Reservation.id)).where(Reservation.service_id == svid)
        ).scalar_one()
        if refs == 0:
            db.delete(svc)
            removed[DEMO_SERVICE] += 1
        else:
            kept[DEMO_SERVICE] += 1

    # 追蹤列全數清除:保留下來的物件已因真實預約「轉正」,不再視為示範。
    for o in objs:
        db.delete(o)

    db.commit()
    return {
        "cleared": True,
        "removed": {k: v for k, v in removed.items()},
        "kept": {k: v for k, v in kept.items() if v},
    }
