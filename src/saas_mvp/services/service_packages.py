"""服務套票：定義、發行、餘額與預約扣次／退次。"""

from __future__ import annotations

import datetime
import secrets
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from saas_mvp.models.customer import Customer
from saas_mvp.models.reservation import Reservation
from saas_mvp.models.service import Service
from saas_mvp.models.service_package import (
    PACKAGE_ACTIVE,
    PACKAGE_CANCELLED,
    CustomerPackage,
    PackageCreditLedger,
    ServicePackage,
    ServicePackageItem,
)
from saas_mvp.services.tenants import tenant_query


class ServicePackageError(ValueError):
    pass


class PackageNotFound(ServicePackageError):
    pass


class PackageHasNoItems(ServicePackageError):
    pass


class PackageCreditUnavailable(ServicePackageError):
    pass


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _aware(value: datetime.datetime) -> datetime.datetime:
    return value.replace(tzinfo=datetime.timezone.utc) if value.tzinfo is None else value


@dataclass(frozen=True)
class WalletCredit:
    customer_package: CustomerPackage
    service: Service
    remaining: int


def list_packages(db: Session, *, tenant_id: int, active_only: bool = False) -> list[ServicePackage]:
    q = tenant_query(db, ServicePackage, tenant_id)
    if active_only:
        q = q.filter(ServicePackage.is_active.is_(True))
    return q.order_by(ServicePackage.is_active.desc(), ServicePackage.id.desc()).all()


def package_items(db: Session, *, tenant_id: int, package_id: int) -> list[ServicePackageItem]:
    return (
        tenant_query(db, ServicePackageItem, tenant_id)
        .filter(ServicePackageItem.package_id == package_id)
        .order_by(ServicePackageItem.id)
        .all()
    )


def create_package(
    db: Session,
    *,
    tenant_id: int,
    name: str,
    description: str | None,
    price_cents: int,
    validity_days: int,
) -> ServicePackage:
    name = name.strip()
    if not name or len(name) > 128:
        raise ServicePackageError("套票名稱為必填，最多 128 字。")
    if not 0 <= price_cents <= 2_000_000_000:
        raise ServicePackageError("售價需介於 NT$0～20,000,000。")
    if not 1 <= validity_days <= 3650:
        raise ServicePackageError("有效天數需介於 1～3650 天。")
    duplicate = (
        tenant_query(db, ServicePackage, tenant_id)
        .filter(func.lower(ServicePackage.name) == name.lower())
        .first()
    )
    if duplicate:
        raise ServicePackageError("已有同名套票。")
    row = ServicePackage(
        tenant_id=tenant_id,
        name=name,
        description=(description or "").strip()[:2000] or None,
        price_cents=price_cents,
        validity_days=validity_days,
    )
    db.add(row)
    db.flush()
    return row


def add_or_update_item(
    db: Session,
    *,
    tenant_id: int,
    package_id: int,
    service_id: int,
    included_quantity: int,
) -> ServicePackageItem:
    if not 1 <= included_quantity <= 999:
        raise ServicePackageError("服務次數需介於 1～999 次。")
    package = (
        tenant_query(db, ServicePackage, tenant_id)
        .filter(ServicePackage.id == package_id)
        .first()
    )
    service = (
        tenant_query(db, Service, tenant_id).filter(Service.id == service_id).first()
    )
    if package is None or service is None:
        raise PackageNotFound("套票或服務不存在。")
    row = (
        tenant_query(db, ServicePackageItem, tenant_id)
        .filter(
            ServicePackageItem.package_id == package_id,
            ServicePackageItem.service_id == service_id,
        )
        .first()
    )
    if row is None:
        row = ServicePackageItem(
            tenant_id=tenant_id,
            package_id=package_id,
            service_id=service_id,
            included_quantity=included_quantity,
        )
        db.add(row)
    else:
        row.included_quantity = included_quantity
    db.flush()
    return row


def set_active(
    db: Session, *, tenant_id: int, package_id: int, active: bool
) -> ServicePackage:
    row = (
        tenant_query(db, ServicePackage, tenant_id)
        .filter(ServicePackage.id == package_id)
        .first()
    )
    if row is None:
        raise PackageNotFound("套票不存在。")
    row.is_active = active
    db.flush()
    return row


def issue_package(
    db: Session,
    *,
    tenant_id: int,
    customer_id: int,
    package_id: int,
    actor_user_id: int | None,
    starts_at: datetime.datetime | None = None,
    issuance_key: str | None = None,
) -> CustomerPackage:
    """發行套票並逐服務寫入正向次數；不 commit。"""
    customer = db.execute(
        select(Customer)
        .where(Customer.id == customer_id, Customer.tenant_id == tenant_id)
        .with_for_update()
    ).scalar_one_or_none()
    package = db.execute(
        select(ServicePackage)
        .where(ServicePackage.id == package_id, ServicePackage.tenant_id == tenant_id)
        .with_for_update()
    ).scalar_one_or_none()
    if customer is None or package is None or not package.is_active:
        raise PackageNotFound("顧客或啟用中的套票不存在。")
    key = (issuance_key or secrets.token_urlsafe(24)).strip()
    if not key or len(key) > 64:
        raise ServicePackageError("套票發行識別碼格式不正確，請重新整理後再試。")
    # customer 行鎖使同一顧客的並發重送序列化；後取得鎖者會看到第一筆，
    # 配合 DB unique(tenant_id, issuance_key) 防多 worker 重複發行。
    existing = (
        tenant_query(db, CustomerPackage, tenant_id)
        .filter(CustomerPackage.issuance_key == key)
        .first()
    )
    if existing is not None:
        if existing.customer_id != customer.id or existing.package_id != package.id:
            raise ServicePackageError("發行請求已被使用，請重新整理頁面後再試。")
        return existing
    items = package_items(db, tenant_id=tenant_id, package_id=package_id)
    if not items:
        raise PackageHasNoItems("套票尚未加入任何服務，不能發行。")
    start = starts_at or _utcnow()
    row = CustomerPackage(
        tenant_id=tenant_id,
        customer_id=customer.id,
        package_id=package.id,
        package_name_snapshot=package.name,
        price_cents_snapshot=package.price_cents,
        issuance_key=key,
        status=PACKAGE_ACTIVE,
        starts_at=start,
        expires_at=start + datetime.timedelta(days=package.validity_days),
        issued_by_user_id=actor_user_id,
    )
    db.add(row)
    db.flush()
    for item in items:
        db.add(
            PackageCreditLedger(
                tenant_id=tenant_id,
                customer_package_id=row.id,
                customer_id=customer.id,
                service_id=item.service_id,
                delta=item.included_quantity,
                kind="issue",
                note=f"發行套票：{package.name}",
                actor_user_id=actor_user_id,
            )
        )
    db.flush()
    return row


def cancel_customer_package(
    db: Session,
    *,
    tenant_id: int,
    customer_id: int,
    customer_package_id: int,
    actor_user_id: int | None,
    note: str | None = None,
) -> CustomerPackage:
    """作廢顧客套票並以帳本沖銷尚未使用次數；不處理金流退款。"""
    owned = db.execute(
        select(CustomerPackage)
        .where(
            CustomerPackage.id == customer_package_id,
            CustomerPackage.tenant_id == tenant_id,
            CustomerPackage.customer_id == customer_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if owned is None:
        raise PackageNotFound("顧客套票不存在。")
    if owned.status == PACKAGE_CANCELLED:
        return owned
    service_ids = [
        sid
        for (sid,) in db.query(PackageCreditLedger.service_id)
        .filter(
            PackageCreditLedger.tenant_id == tenant_id,
            PackageCreditLedger.customer_package_id == owned.id,
        )
        .distinct()
    ]
    for service_id in service_ids:
        remaining = _balance(
            db,
            tenant_id=tenant_id,
            customer_package_id=owned.id,
            service_id=service_id,
        )
        if remaining > 0:
            db.add(
                PackageCreditLedger(
                    tenant_id=tenant_id,
                    customer_package_id=owned.id,
                    customer_id=customer_id,
                    service_id=service_id,
                    delta=-remaining,
                    kind="adjust",
                    note=(note or "套票作廢，沖銷未用次數")[:255],
                    actor_user_id=actor_user_id,
                )
            )
    owned.status = PACKAGE_CANCELLED
    owned.cancelled_at = _utcnow()
    db.flush()
    return owned


def _balance(
    db: Session, *, tenant_id: int, customer_package_id: int, service_id: int
) -> int:
    value = db.execute(
        select(func.coalesce(func.sum(PackageCreditLedger.delta), 0)).where(
            PackageCreditLedger.tenant_id == tenant_id,
            PackageCreditLedger.customer_package_id == customer_package_id,
            PackageCreditLedger.service_id == service_id,
        )
    ).scalar_one()
    return int(value or 0)


def customer_wallet(
    db: Session, *, tenant_id: int, customer_id: int, include_empty: bool = False
) -> list[WalletCredit]:
    now = _utcnow()
    packages = (
        tenant_query(db, CustomerPackage, tenant_id)
        .filter(
            CustomerPackage.customer_id == customer_id,
            CustomerPackage.status == PACKAGE_ACTIVE,
        )
        .order_by(CustomerPackage.expires_at, CustomerPackage.id)
        .all()
    )
    services = {
        s.id: s for s in tenant_query(db, Service, tenant_id).all()
    }
    out: list[WalletCredit] = []
    for owned in packages:
        if _aware(owned.starts_at) > now or _aware(owned.expires_at) < now:
            continue
        service_ids = [
            sid
            for (sid,) in db.query(PackageCreditLedger.service_id)
            .filter(
                PackageCreditLedger.tenant_id == tenant_id,
                PackageCreditLedger.customer_package_id == owned.id,
            )
            .distinct()
        ]
        for service_id in service_ids:
            service = services.get(service_id)
            if service is None:
                continue
            remaining = _balance(
                db,
                tenant_id=tenant_id,
                customer_package_id=owned.id,
                service_id=service_id,
            )
            if remaining > 0 or include_empty:
                out.append(WalletCredit(owned, service, remaining))
    return out


def eligible_credit_count(
    db: Session, *, tenant_id: int, customer_id: int, service_id: int
) -> int:
    return sum(
        credit.remaining
        for credit in customer_wallet(db, tenant_id=tenant_id, customer_id=customer_id)
        if credit.service.id == service_id
    )


def redeem_for_reservation(
    db: Session,
    *,
    tenant_id: int,
    customer_id: int,
    service_id: int,
    reservation: Reservation,
    customer_package_id: int | None = None,
) -> PackageCreditLedger:
    """鎖定候選套票並扣 1 次；優先使用最早到期者，不 commit。"""
    existing = (
        tenant_query(db, PackageCreditLedger, tenant_id)
        .filter(
            PackageCreditLedger.reservation_id == reservation.id,
            PackageCreditLedger.kind == "redeem",
        )
        .first()
    )
    if existing is not None:
        return existing
    now = _utcnow()
    query = (
        select(CustomerPackage)
        .where(
            CustomerPackage.tenant_id == tenant_id,
            CustomerPackage.customer_id == customer_id,
            CustomerPackage.status == PACKAGE_ACTIVE,
            CustomerPackage.starts_at <= now,
            CustomerPackage.expires_at >= now,
        )
        .order_by(CustomerPackage.expires_at, CustomerPackage.id)
        .with_for_update()
    )
    if customer_package_id is not None:
        query = query.where(CustomerPackage.id == customer_package_id)
    for owned in db.execute(query).scalars():
        if _balance(
            db,
            tenant_id=tenant_id,
            customer_package_id=owned.id,
            service_id=service_id,
        ) < 1:
            continue
        row = PackageCreditLedger(
            tenant_id=tenant_id,
            customer_package_id=owned.id,
            customer_id=customer_id,
            service_id=service_id,
            reservation_id=reservation.id,
            delta=-1,
            kind="redeem",
            note="預約使用套票",
        )
        db.add(row)
        db.flush()
        return row
    raise PackageCreditUnavailable("沒有可用的此服務套票次數。")


def refund_for_cancelled_reservation(
    db: Session, *, tenant_id: int, reservation_id: int
) -> PackageCreditLedger | None:
    """若預約曾扣套票則退 1 次；重複取消為冪等。"""
    redeem = (
        tenant_query(db, PackageCreditLedger, tenant_id)
        .filter(
            PackageCreditLedger.reservation_id == reservation_id,
            PackageCreditLedger.kind == "redeem",
        )
        .first()
    )
    if redeem is None:
        return None
    existing = (
        tenant_query(db, PackageCreditLedger, tenant_id)
        .filter(
            PackageCreditLedger.reservation_id == reservation_id,
            PackageCreditLedger.kind == "refund",
        )
        .first()
    )
    if existing is not None:
        return existing
    row = PackageCreditLedger(
        tenant_id=tenant_id,
        customer_package_id=redeem.customer_package_id,
        customer_id=redeem.customer_id,
        service_id=redeem.service_id,
        reservation_id=reservation_id,
        delta=1,
        kind="refund",
        note="預約取消自動退回",
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        # 真正並發重複取消由唯一約束守住；交由外層交易重試/回滾。
        raise
    return row


def ledger_for_customer(
    db: Session, *, tenant_id: int, customer_id: int, limit: int = 50
) -> list[PackageCreditLedger]:
    return (
        tenant_query(db, PackageCreditLedger, tenant_id)
        .filter(PackageCreditLedger.customer_id == customer_id)
        .order_by(PackageCreditLedger.id.desc())
        .limit(limit)
        .all()
    )
