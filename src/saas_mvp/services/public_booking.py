"""公開常駐網路預約(R12-A)— /p/{slug}/book,無 token、無登入。

與 tokenized 表單(services/booking_form)的差異:
* 入口常駐:店家 opt-in(profile.online_booking_enabled)+ WEB_BOOKING
  feature + is_published 三閘皆過才對外露出;未開一律 404 不洩漏存在性。
* 身分=訪客自填姓名+電話:電話正規化後對本租戶 walk-in 客
  (line_user_id IS NULL)去重併檔;LINE 客不參與電話比對(避免陌生人
  輸入他人電話掛上 LINE 身分)。email 僅在「新建」顧客時寫入 —— 既有
  顧客檔一律不以訪客輸入覆寫(防止以電話冒名改寫他人聯絡方式)。
* 無候補:候補通知走 LINE push,匿名網頁客收不到,額滿即回額滿。
* 步驟資料組裝(服務/日期/時段/員工)全數複用 booking_form service。

建單走既有 book_slot(customer_id=...):原子容量/跨租戶防護全複用;
customer_id 路徑不含黑名單檢查,本服務在併檔時自行檢查。
"""

from __future__ import annotations

import datetime
import re

from sqlalchemy.orm import Session

from saas_mvp.models.business_profile import BusinessProfile
from saas_mvp.models.customer import Customer
from saas_mvp.models.reservation import Reservation
from saas_mvp.services import features as features_svc
from saas_mvp.services import profile as profile_svc
from saas_mvp.services.tenants import tenant_query

# 建單備註:店家在預約列表看得到來源;同時是防灌單計數的查詢鍵。
WEB_BOOKING_NOTE = "網路預約"

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class PublicBookingError(Exception):
    """公開預約 domain 錯誤(訊息可直接顯示給訪客)。"""


def normalize_phone(raw: str) -> str:
    """電話正規化:去分隔符、+886 開頭轉 0。8–15 碼數字,否則拒絕。"""
    digits = re.sub(r"[\s\-().]", "", (raw or "").strip())
    if digits.startswith("+886"):
        digits = "0" + digits[4:]
    elif digits.startswith("886") and len(digits) >= 11:
        digits = "0" + digits[3:]
    if not digits.isdigit() or not (8 <= len(digits) <= 15):
        raise PublicBookingError("請填寫正確的聯絡電話。")
    return digits


def resolve_entry(db: Session, slug: str) -> BusinessProfile | None:
    """三閘皆過回 profile,否則 None(router 一律 404 不洩漏存在性)。"""
    profile = profile_svc.get_by_slug(db, slug)
    if profile is None:
        return None
    if not getattr(profile, "online_booking_enabled", False):
        return None
    if not features_svc.is_enabled(
        db, profile.tenant_id, features_svc.WEB_BOOKING
    ):
        return None
    return profile


def _resolve_customer(
    db: Session,
    *,
    tenant_id: int,
    name: str,
    phone: str,
    email: str | None,
) -> tuple[Customer, bool]:
    """電話去重併檔 walk-in 客;無則新建。黑名單客拒絕(訊息不點破名單)。

    回傳 (customer, created):created=True 表本次新建 —— portal 連結只發給
    新建檔(檔內只有訪客剛輸入的內容);併檔到既有客不發(輸入他人電話即
    可看該客完整歷史=冒名資訊洩漏)。

    黑名單檢查跨「所有」電話相符的客(含 LINE 客):被拉黑的 LINE 客走匿名
    管道用同一支電話重約,book_slot 的 customer_id 路徑不會再攔,必須在這裡
    擋。只檢查、不併檔 —— 併檔仍限 walk-in,不開冒名洞。
    """
    blacklisted_match = (
        tenant_query(db, Customer, tenant_id)
        .filter(Customer.phone == phone, Customer.blacklisted.is_(True))
        .first()
    )
    if blacklisted_match is not None:
        raise PublicBookingError("目前無法完成預約,請直接與店家聯繫。")
    existing = (
        tenant_query(db, Customer, tenant_id)
        .filter(Customer.phone == phone, Customer.line_user_id.is_(None))
        .order_by(Customer.id)
        .first()
    )
    if existing is not None:
        # 既有檔不以訪客輸入覆寫(姓名/email 皆保留店家所存);
        # booking_count/last_booked_at 由 book_slot 維護。
        return existing, False
    customer = Customer(
        tenant_id=tenant_id,
        line_user_id=None,
        display_name=name.strip()[:128],
        phone=phone,
        email=(email or None),
        booking_count=0,  # book_slot 的 customer_id 路徑會 +1
        last_booked_at=None,
    )
    db.add(customer)
    db.flush()
    return customer, True


def _flood_gate(db: Session, tenant_id: int) -> None:
    """匿名端點防灌單:每租戶每小時網路預約建單上限(IP 限流之外第二層)。"""
    hour_ago = _utcnow() - datetime.timedelta(hours=1)
    recent = (
        tenant_query(db, Reservation, tenant_id)
        .filter(
            Reservation.created_at >= hour_ago,
            Reservation.note == WEB_BOOKING_NOTE,
        )
        .count()
    )
    if recent >= 30:
        raise PublicBookingError("目前預約人數眾多,請稍後再試。")


def submit(
    db: Session,
    *,
    tenant_id: int,
    slot_id: int,
    party_size: int,
    service_id: int | None,
    staff_id: int | None,
    name: str,
    phone: str,
    email: str | None,
):
    """驗輸入 → 防灌單 → 併檔/建檔 → book_slot 建單。

    回傳 (reservation, customer, created):created 語意見 _resolve_customer。
    book_slot 的 domain error(額滿/查無時段)原樣向上拋,由 router 轉
    友善頁面。輸入問題拋 PublicBookingError(訊息可直接顯示)。
    """
    name = (name or "").strip()
    if not name or len(name) > 128:
        raise PublicBookingError("請填寫姓名。")
    norm_phone = normalize_phone(phone)
    clean_email = (email or "").strip() or None
    if clean_email is not None and (
        len(clean_email) > 255 or not _EMAIL_RE.fullmatch(clean_email)
    ):
        raise PublicBookingError("Email 格式不正確(可留空)。")

    _flood_gate(db, tenant_id)
    customer, created = _resolve_customer(
        db, tenant_id=tenant_id, name=name, phone=norm_phone, email=clean_email
    )

    from saas_mvp.services import booking as booking_svc

    resv = booking_svc.book_slot(
        db,
        tenant_id=tenant_id,
        slot_id=slot_id,
        party_size=party_size,
        customer_id=customer.id,
        service_id=service_id,
        staff_id=staff_id,
        note=WEB_BOOKING_NOTE,
        # 線上來源:套用租戶定金政策。book_slot 預設以 line_user_id 判定
        # 線上與否,本管道無 LINE 身分,必須明示 —— 否則最需要定金防
        # no-show 的匿名管道反而全免定金(對抗審查揪出)。
        require_deposit=True,
    )
    return resv, customer, created
